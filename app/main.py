"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import base64
import concurrent.futures
import hashlib
import io
import requests
import json
import os
import re
import sys
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus

from flask import (Flask, Response, abort, jsonify, make_response, redirect,
                   render_template_string, request, session, send_from_directory)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, evaluate_garment,
                           generate_capsule, generate_card_palette,
                           generate_directions, generate_personality_portrait,
                           generate_shopping_list, generate_styling_pair,
                           refine_colortype_subtype, refine_substyle,
                           render_flatlay, render_look_on_client)
from core.tracking import (approved_feedback, chat_log, count_generations, count_generations_ip,
                           count_today, feedback_list, funnel, gap_progress, gap_summary, leads,
                           progress, record_call, record_chat, record_consent, record_event,
                           record_feedback, record_generation, record_session, set_feedback_approved)
from core.auth import email_configured, make_token, read_token, send_magic_link
from core.figure_rules import fit_rules_client
from core.chat import stylist_reply
from core.catalog import match_products, parse_csv, score_products
from core.weather import configured as weather_configured, dress_advice, get_weather
from core.canon import enforce_substyles
from core.item_images import item_image_url, item_type
from core.profiles import (add_wardrobe_item, card_link_token, current_card_by_season,
                           delete_wardrobe_item, get_profile, merge_profile, save_card,
                           save_diagnosis, save_style_profile, user_by_card_token,
                           wardrobe_items)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # дизайнерский сайт (статика)
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим в квизе (контроль стоимости/времени)
# Сколько образов генерим одновременно. Больше — быстрее, но каждый воркер держит картинку в
# памяти; на маленьком контейнере это кончается убитым процессом и потерянной Картой.
RENDER_WORKERS = int(os.getenv("SENSE_RENDER_WORKERS", "3"))
DEMO_DAILY_LIMIT = int(os.getenv("DEMO_DAILY_LIMIT", "40"))  # защита от слива ключа
# Каждая вещь гардероба — отдельный vision-вызов. Пачку ограничиваем: 20 фото это и минуты
# ожидания у экрана, и заметный расход квоты ключа за один клик.
MAX_WARDROBE_UPLOAD = int(os.getenv("SENSE_MAX_WARDROBE_UPLOAD", "8"))

# статика сайта раздаётся из web/ в корне; зарегистрированные роуты (/demo, /api…) важнее
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
# Предметное фото по названию вещи. Фильтр, а не поле Карты: у собранных ранее Карт поля нет,
# а картинка нужна и им. Генерации здесь не происходит — только поиск готового кадра.
app.jinja_env.filters["item_img"] = item_image_url
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # лимит загрузки 15 МБ
# секрет сессий/magic-link: env SENSE_SECRET_KEY или стабильный файл на постоянном томе
from core.config import secret_key as _secret_key, data_dir as _data_dir  # noqa: E402
app.secret_key = _secret_key()

# ── Личность без регистрации ───────────────────────────────────────────────────────────────────
# Клиентка проходит весь путь (квиз → Карта → кабинет) без почты. Раньше барьер стоял на /card:
# после квиза её выбрасывало на /login, и путь обрывался — а на проде без ключей UniSender письмо
# и вовсе не уходило. Идентичность держим в подписанной сессионной cookie: `anon-<hex>`. Ключ
# профиля — произвольная строка (core/profiles.py), поэтому анонимный id ложится в хранилище
# ровно как email, без миграции схемы. Почта остаётся опцией «сохранить результат».
ANON_SESSION_DAYS = int(os.getenv("SENSE_ANON_SESSION_DAYS", "365"))
app.permanent_session_lifetime = timedelta(days=ANON_SESSION_DAYS)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,   # cookie сессии недоступна из JS
    SESSION_COOKIE_SAMESITE="Lax",  # не уезжает на сторонние запросы
    # На проде (Amvera — примонтирован /data) сессия только по HTTPS. Локально выключено,
    # иначе cookie не встанет на http://127.0.0.1 и весь путь не проверить.
    SESSION_COOKIE_SECURE=os.getenv("SENSE_COOKIE_SECURE", "1" if Path("/data").is_dir() else "0") == "1",
)


def _current_user() -> str:
    """Кто сейчас на сайте: email (если вошла по ссылке) либо стабильный анонимный id.

    Единая точка идентичности для пользовательских маршрутов. Никогда не пустая строка —
    поэтому лимит бесплатных генераций считается и для анонима (см. `_gen_allowed`).
    """
    email = session.get("email")
    if email:
        return email
    anon = session.get("anon")
    if not anon:
        anon = "anon-" + uuid.uuid4().hex
        session["anon"] = anon
    session.permanent = True  # переживает закрытие браузера — иначе Карта «теряется»
    return anon


def _attach_quiz_diagnosis(user: str) -> None:
    """Привязать к пользователю диагноз, посчитанный квизом.

    Квиз считает диагностику анонимно и кладёт её под `job_id` (в памяти и на диске), а не под
    пользователем. Без этой привязки человек с готовой Формулой упирается в «сначала диагностика»:
    так было в /card/build, куда клиентка приходит из формы сборки Карты.

    from_job — из CTA квиза; last_job — из сессии, страховка для тарифных кнопок, которые ведут
    на /card без ?from_job=.
    """
    job = request.args.get("from_job") or session.get("last_job")
    if not job:
        return
    # из памяти (быстро) или с диска (переживает рестарт сервера)
    job_diag = (_JOBS.get(job) or {}).get("diag") or _load_pending_diag(job)
    if job_diag:
        save_diagnosis(user, job_diag)


def _is_anon(user: str) -> bool:
    return (user or "").startswith("anon-")


def _display_name(user: str) -> str:
    """Как показать пользователя в интерфейсе. Технический `anon-<hex>` наружу не выносим:
    в шаблонах он вылезал бы как «для anon-a1b2c3…». У анонима имени просто нет."""
    return "" if _is_anon(user) else (user or "")


@app.after_request
def _cors(resp):  # чтобы статический квиз с Vercel мог звать /api/analyze
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


# Виджет чат-стилиста на ВСЕ HTML-страницы (кроме самого чата и логина) — одной инъекцией,
# чтобы не дублировать <script> в каждом шаблоне. Уже включающие виджет (лендинг/квиз) пропускаем.
_WIDGET_SKIP = {"/stylist", "/login"}


@app.after_request
def _inject_widget(resp):
    try:
        ct = resp.content_type or ""
        if ct.startswith("text/html") and request.path not in _WIDGET_SKIP:
            body = resp.get_data(as_text=True)
            if "</body>" in body and "stylist-widget.js" not in body:
                resp.set_data(body.replace(
                    "</body>", '<script src="/stylist-widget.js" defer></script></body>', 1))
    except Exception:  # noqa: BLE001 — инъекция не должна ронять ответ
        pass
    return resp


_NO_CACHE_PATHS = (
    "/card",
    "/cabinet",
    "/build-card",
    "/login",
    "/me",
    "/tariffs",
    "/api/build-status",
    "/api/card-feedback",
)


@app.after_request
def _no_store_dynamic(resp):
    """Динамические страницы Карты/кабинета нельзя кешировать.

    После деплоя браузер и промежуточные прокси иногда держали старый HTML по cookie-сессии:
    клиентка видела старое меню и старые блоки, хотя новый код уже был на сервере.
    Для живых экранов продукта отдаём `no-store`, чтобы всегда брать актуальную версию.
    """
    path = request.path or ""
    if path.startswith(_NO_CACHE_PATHS):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        resp.headers["Vary"] = "Cookie"
    return resp

FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sense Style AI</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 /* Бренд-шрифты, а не Georgia: эта страница показывается клиентке при ошибке загрузки, и
    системный сериф рядом с остальным продуктом читается как чужой сайт. */
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;max-width:640px;margin:0 auto;padding:28px 20px 70px;background:var(--cream);color:var(--ink);line-height:1.55}
 .top{display:flex;justify-content:space-between;align-items:center}
 .logo{font-family:'Cormorant Garamond',serif;font-size:20px;letter-spacing:.5px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:32px;margin:14px 0 4px}
 label{display:block;margin:16px 0 5px;font-size:14px;color:var(--muted)}
 input,select,textarea{width:100%;padding:11px;border:1px solid #d9d2c7;border-radius:6px;font:inherit;background:#fff;box-sizing:border-box}
 button{margin-top:24px;padding:14px 30px;background:var(--wine);color:#fff;border:0;border-radius:6px;font:inherit;font-size:16px;cursor:pointer}
 button:hover{opacity:.92}
 .hint{color:var(--muted);font-size:13px} .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:6px}
</style></head><body>
<div class=top><span class=logo>Чувство стиля</span><span><a href="/me" style="margin-right:14px">Мой профиль</a><a href="/">← на главную</a></span></div>
<h1>Диагностика стиля</h1>
<p class=hint>Загрузи фото в полный рост и ответь на несколько вопросов — определим Формулу стиля и покажем тебя в новых образах.</p>
{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/analyze" enctype="multipart/form-data">
 <label>Имя или email (чтобы отслеживать динамику)</label><input name=client value="" placeholder="anna@example.com">
 <label>Фото (портрет/в полный рост)</label><input type=file name=photo accept="image/*" required>
 <label>Рост, см</label><input type=number name=height value=168>
 <label>Возраст</label><input type=number name=age value=38>
 <label>Чем занимаешься</label><input name=profession placeholder="например: руководитель отдела">
 <label>Как тебя считывают сейчас (через запятую)</label><input name=now_traits required placeholder="например: сдержанная, простая, незаметная">
 <label>Как хочешь, чтобы считывали — топ-3 (через запятую)</label><input name=want_traits required placeholder="например: уверенная, элегантная, тёплая">
 <p class=hint style="margin:16px 0 0">Цветотип, контраст и силуэт фигуры определим сами по фото — указывать не нужно.</p>
 <label>Сегмент бюджета</label>
 <select name=price>
  <option value="middle">Средний</option>
  <option value="low">Масс-маркет</option>
  <option value="high">Премиум</option>
  <option value="luxury">Люкс</option>
 </select>
 <label>Табу — что точно не наденешь (через запятую)</label><input name=taboos value="">
 <label style="font-weight:normal;font-size:13px;margin-top:16px;display:flex;gap:8px"><input type=checkbox name=consent_processing required style="width:auto"> Согласна на обработку персональных данных согласно <a href="/privacy" target="_blank" rel="noopener">Политике</a>.</label>
 <label style="font-weight:normal;font-size:13px;display:flex;gap:8px"><input type=checkbox name=consent_transfer required style="width:auto"> Согласна на трансграничную передачу фото в ИИ-сервисы (Google, США) для генерации образов.</label>
 <button>Построить образы</button>
<p class=hint>Сборка занимает ~1–2 минуты: анализ фото, диагностика и лукбук образов.</p>
</form>
<p class=hint style="margin-top:24px;border-top:1px solid #e3dccf;padding-top:18px">Уже знаешь свою Формулу и стоишь в магазине? <a href="/garment" style="color:#5D2230">Проверь вещь по фото: брать или не брать →</a></p>
</body></html>"""

RESULT = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Твоя Формула стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
*{box-sizing:border-box}
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;max-width:920px;margin:0 auto;padding:28px 20px 70px;background:var(--cream);color:var(--ink);line-height:1.55}
 /* Число индекса — «голос бренда»: сериф, как заголовки. Georgia здесь выбивалась из продукта. */
 h1,h2{font-family:'Cormorant Garamond',serif;font-weight:600} .gap{font-family:'Cormorant Garamond',serif;font-size:46px;color:var(--wine)} .formula{font-family:'Cormorant Garamond',serif;font-size:24px}
 .looks{display:flex;gap:18px;flex-wrap:wrap;margin-top:18px}
 .look{flex:1 1 260px} .look img{width:100%;border-radius:8px} .desc{font-size:14px;color:#444}
 .meta{color:var(--muted);font-size:14px} a{color:var(--wine)}
</style></head><body>
<p><a href="/demo">← заново</a> · <a href="/">на главную</a></p>
<h1>Твоя Формула стиля</h1>
<p class=formula><b>{{ formula }}</b></p>
<p>Индекс настройки образа: <span class=gap>{{ gap }}%</span> — зона между тем, как тебя считывают сейчас, и тем, как ты хочешь.</p>
{% if prog and prog.sessions > 1 and prog.delta is not none %}
<p class=meta>Динамика имиджа: было {{ prog.first_gap }}% → стало {{ prog.last_gap }}% ({{ '−' ~ prog.delta if prog.delta >= 0 else '+' ~ (-prog.delta) }} п.п. за {{ prog.sessions }} сессии).</p>
{% endif %}
<p>{{ dna }}</p>
<p class=meta>Цветотип: {{ colortype }} · Фигура: {{ figure }} · В капсуле {{ items }} вещей.</p>
<h2>Ты в новых образах</h2>
<p class=meta>Это короткое превью — 2 образа. Полная Карта стиля собирает выверенную палитру, силуэты, стоп-лист и 6 образов под твои сценарии, с PDF к шкафу.</p>
<div class=looks>
 {% for lk in looks %}
 <div class=look>{% if lk.img %}<img src="{{ lk.img }}" alt="Образ">{% endif %}<p class=desc>{{ lk.desc }}</p></div>
 {% endfor %}
</div>
<div style="margin-top:34px;padding-top:22px;border-top:1px solid #d9d2c7">
 <h2 style="margin:0 0 12px">Что дальше</h2>
 <a href="/card" style="display:inline-block;background:var(--wine);color:#fff;text-decoration:none;padding:14px 26px;border-radius:8px;font-size:16px;margin:0 10px 10px 0">Собрать полную Карту стиля →</a>
 <a href="/garment" style="display:inline-block;background:#fff;color:var(--wine);border:1px solid var(--wine);text-decoration:none;padding:14px 26px;border-radius:8px;font-size:16px;margin:0 10px 10px 0">Проверить вещь перед покупкой</a>
 <a href="/me" style="display:inline-block;color:var(--muted);text-decoration:none;padding:14px 0;font-size:15px">Мой профиль</a>
</div></body></html>"""


LANDING = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sense Style — стиль, в котором ты настоящая</title>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 *{box-sizing:border-box} body{margin:0;font-family:Onest,-apple-system,Segoe UI,sans-serif;background:var(--cream);color:var(--ink);line-height:1.6}
 .wrap{max-width:880px;margin:0 auto;padding:0 22px}
 header{padding:22px 0;display:flex;justify-content:space-between;align-items:center}
 .logo{font-size:20px;letter-spacing:.5px}
 .hero{padding:56px 0 36px;text-align:center}
 .hero h1{font-weight:normal;font-size:40px;line-height:1.2;margin:0 0 18px}
 .hero p{font-size:19px;color:var(--muted);max-width:620px;margin:0 auto 30px}
 .btn{display:inline-block;background:var(--wine);color:#fff;text-decoration:none;padding:15px 34px;border-radius:6px;font-size:17px}
 .btn.sm{padding:9px 18px;font-size:14px}
 h2{font-weight:normal;font-size:26px;margin-top:52px}
 .flow{background:#fff;border-radius:10px;padding:8px 28px;margin-top:18px;font-size:17px}
 .flow div{padding:11px 0;border-bottom:1px solid #efece6} .flow div:last-child{border:0}
 .cols{display:flex;gap:18px;flex-wrap:wrap;margin-top:18px}
 .card{flex:1 1 240px;background:#fff;border-radius:10px;padding:20px 22px}
 .card h3{font-weight:normal;font-size:18px;margin:0 0 8px}
 .card p{margin:0;color:var(--muted);font-size:15px}
 .cta{text-align:center;padding:52px 0}
 .sci{color:var(--muted);font-size:15px;margin-top:14px}
 footer{background:#1A1A1A;color:rgba(255,255,255,.6);margin-top:56px;padding:26px 0;font-size:14px}
 footer a{color:rgba(255,255,255,.85)}
</style></head><body>
<div class=wrap>
 <header><div class=logo>Sense&nbsp;Style</div><a class="btn sm" href="/demo">Пройти диагностику</a></header>
 <section class=hero>
  <h1>Стиль, в котором ты&nbsp;— настоящая</h1>
  <p>Персональный fashion-tech стилист на основе психологии моды. Загрузи фото и ответь на несколько вопросов — определим твою Формулу стиля, измерим индекс настройки образа и соберём для тебя визуальные сценарии.</p>
  <a class=btn href="/demo">Построить свои образы →</a>
 </section>

 <h2>Как это работает</h2>
 <div class=flow>
  <div>1. Фото + короткий квиз</div>
  <div>2. <b>Индекс настройки образа, %</b> — зона между «как считывают» и «как хочешь»</div>
  <div>3. Твоя <b>Формула стиля</b> по авторской методологии</div>
  <div>4. Капсула и образы — <b>на тебе</b>, с твоим лицом и фигурой</div>
  <div>5. Список покупок под бюджет и стоп-лист «не покупать»</div>
  <div>6. Трекер: как индекс настройки образа меняется со временем</div>
 </div>

 <h2>Что нас отличает</h2>
 <div class=cols>
  <div class=card><h3>Психология, не мода</h3><p>Образ работает на настройку твоего визуального впечатления, а не на тренд ради тренда.</p></div>
  <div class=card><h3>Измеримый результат</h3><p>Индекс настройки образа в % — видно, как меняется впечатление о тебе.</p></div>
  <div class=card><h3>Это ты, а не модель</h3><p>Образы генерируются на твоём фото — лицо и фигура сохраняются.</p></div>
 </div>
 <p class=sci>В основе — исследования о связи одежды, восприятия и самоощущения (теория самонесоответствия Хиггинса, enclothed cognition).</p>

 <section class=cta>
  <h2 style="margin-top:0">Посмотри на себя в своей Формуле</h2>
  <a class=btn href="/demo">Пройти диагностику →</a>
 </section>
</div>
<footer><div class=wrap>© 2026 «Чувство стиля» · Санкт-Петербург · <a href="/privacy">Политика конфиденциальности</a></div></footer>
</body></html>"""

PRIVACY = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Политика конфиденциальности — Sense Style</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
*{box-sizing:border-box}body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;max-width:820px;margin:0 auto;padding:40px 22px 80px;background:#F5EFE3;color:#1f1d1b;line-height:1.6}
 h1{font-weight:normal;font-size:30px} h2{font-weight:normal;font-size:20px;margin-top:30px} .muted{color:#777;font-size:14px} a{color:#2b2b2b}</style>
</head><body>
<p><a href="/">← на главную</a></p>
<h1>Политика конфиденциальности</h1>
<p class=muted>Сервис Sense Style. Редакция от 27.06.2026. Перед коммерческим запуском текст проходит юридическую проверку (152-ФЗ).</p>
<h2>1. Оператор</h2><p>ИП Колупаева Ксения Викторовна (Санкт-Петербург, РФ), ИНН 510705615187. Контакт: sense-style.ru@yandex.ru.</p>
<h2>2. Какие данные собираем</h2><p>Ответы диагностики (квиз); фотографию для анализа и генерации образов; по желанию — имя/email; технические данные (IP, время) и факт согласия.</p>
<h2>3. Цели</h2><p>Стилевая диагностика и расчёт Identity Gap, генерация образов, сохранение результата и динамики (при наличии контактов), поддержка.</p>
<h2>4. Правовые основания</h2><p>Согласие пользователя, исполнение договора, требования законодательства РФ.</p>
<h2>5. Хранение и обработка</h2><p>Данные граждан РФ хранятся в базах на территории РФ (ст. 18 ч. 5 152-ФЗ). Фотография обрабатывается эфемерно и не сохраняется после обработки; в базе остаются результаты и история Identity Gap. Переписка с ИИ-стилистом сохраняется для улучшения качества сервиса. Передача по HTTPS, доступ ограничен, факт согласия журналируется.</p>
<h2>6. Передача третьим лицам</h2><p>Для генерации образов привлекается AI-обработчик (Google, Gemini); при оплате — платёжный провайдер. При отдельном согласии на рассылку email передаётся сервису email-рассылок для отправки писем о стиле и новинках; согласие можно отозвать в любой момент (ссылка «отписаться» в каждом письме или запрос на почту оператора). Данные не используются для обучения сторонних моделей и не передаются третьим лицам в их интересах.</p>
<h2>7. Трансграничная передача</h2><p>Фотография передаётся AI-сервису за пределами РФ (США) только при наличии отдельного согласия и после выполнения требований о локализации и уведомлении Роскомнадзора.</p>
<h2>8. Сроки хранения</h2><p>Фото — не хранится после обработки; результаты и история — до отзыва согласия/запроса на удаление.</p>
<h2>9. Права пользователя</h2><p>Доступ, уточнение, удаление данных, отзыв согласия — по запросу на sense-style.ru@yandex.ru, ответ до 10 рабочих дней.</p>
<h2>10. Cookie и изменения</h2><p>Сервис может использовать технические и аналитические cookie. Оператор вправе изменять Политику; новая редакция действует с момента публикации.</p>
</body></html>"""


GARMENT_FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Брать или не брать — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:600px;margin:0 auto;padding:22px 20px 70px}
 .top{display:flex;justify-content:space-between;align-items:center}
 .logo{font-size:18px;letter-spacing:.5px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .eyebrow{font-family:Arial,sans-serif;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--wine);margin:30px 0 10px}
 h1{font-family:'Cormorant Garamond',serif;font-weight:normal;font-size:34px;line-height:1.12;margin:0 0 12px}
 .lead{font-size:17px;color:var(--muted);margin:0 0 8px}
 .steps{display:flex;gap:10px;margin:22px 0 6px}
 .step{flex:1;background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 14px;font-size:13px;color:#4a443c}
 .step b{display:block;color:var(--wine);font-size:18px;margin-bottom:3px}
 .card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:8px 22px 26px;margin-top:18px}
 label{display:block;margin:20px 0 6px;font-size:14px;color:var(--ink)}
 .sub{font-size:12px;color:var(--muted);font-weight:normal}
 input,select{width:100%;padding:12px;border:1px solid #d9d2c7;border-radius:8px;font:inherit;background:#fff}
 select{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%235D2230' fill='none' stroke-width='1.5'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center}
 .file{border:1.5px dashed #cdbfa6;border-radius:10px;padding:16px;text-align:center;background:#fbf8f1}
 button{margin-top:26px;width:100%;padding:15px;background:var(--wine);color:#fff;border:0;border-radius:10px;font:inherit;font-size:17px;cursor:pointer}
 button:hover{opacity:.93}
 .consent{font-size:13px;color:var(--muted);display:flex;gap:8px;margin-top:14px;line-height:1.4} .consent input{width:auto;margin-top:3px}
 .hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px}
 .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:8px}
 .group{margin-top:18px;padding-top:16px;border-top:1px solid var(--line)} .group:first-of-type{border-top:0;padding-top:0}
 .gtitle{font-family:Arial,sans-serif;font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--wine);margin-bottom:6px}
 .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:4px}
 .chip{display:inline-flex;cursor:pointer} .chip input{position:absolute;opacity:0;pointer-events:none;width:0;height:0}
 .chip span{display:inline-block;padding:9px 14px;border:1px solid #d9d2c7;border-radius:999px;font-size:13.5px;color:#4a443c;background:#fff;transition:.15s}
 .chip input:checked+span{background:var(--wine);color:#fff;border-color:var(--wine)}
 .note{background:#eef6ee;border:1px solid #cfe3cf;border-radius:10px;padding:11px 14px;font-size:13.5px;color:#3a5a3a;margin-top:8px}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><span><a href="/me" style="margin-right:14px">Мой профиль</a><a href="/">← на главную</a></span></div>

<div class=eyebrow>Проверка вещи</div>
<h1>Брать или не брать?</h1>
<p class=lead>Стоишь в примерочной и сомневаешься? Сфоткай вещь — и узнай за пару секунд, работает ли она на твой образ. Чтобы не покупать то, что потом висит с биркой.</p>

<div class=steps>
 <div class=step><b>1</b>Фото вещи</div>
 <div class=step><b>2</b>Профиль стиля</div>
 <div class=step><b>3</b>Честный вердикт</div>
</div>

{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/garment/check" enctype="multipart/form-data">
<div class=card>
 <label>Фото вещи</label>
 <div class=file><input type=file name=photo accept="image/*" required></div>

 <div class=note id=savednote style="display:none">Твой профиль стиля сохранён — можно сразу загрузить фото. Изменить ответы можно ниже.</div>

 <div class=group><div class=gtitle>1 · Линии и посадка</div>
  <label>Какие линии силуэта тебе ближе</label>
  <select name=silhouette_lines>
   <option value="">— выбери —</option>
   <option value="straight">Прямые и чёткие</option>
   <option value="soft">Мягкие и округлые</option>
   <option value="balanced">Сбалансированные</option>
  </select>
  <label>Что любишь подчёркивать</label>
  <select name=fit_focus>
   <option value="">— выбери —</option>
   <option value="waist">Талию</option>
   <option value="relaxed">Свободу, оверсайз</option>
   <option value="elongate">Вертикаль, удлинённый силуэт</option>
  </select>
  <label>Где вещи обычно сидят плохо <span class=sub>— можно несколько</span></label>
  <div class=chips>
   <label class=chip><input type=checkbox name=fit_challenges value=waist_gap><span>Велико в талии, если впору в бёдрах</span></label>
   <label class=chip><input type=checkbox name=fit_challenges value=tight_shoulders><span>Узко в плечах</span></label>
   <label class=chip><input type=checkbox name=fit_challenges value=short_sleeves><span>Коротки рукава</span></label>
   <label class=chip><input type=checkbox name=fit_challenges value=long_torso><span>Длинный торс / короткие ноги</span></label>
  </div>
 </div>

 <div class=group><div class=gtitle>2 · ДНК стиля</div>
  <label>3 слова о твоём идеальном образе <span class=sub>— до трёх</span></label>
  <div class=chips>
   <label class=chip><input type=checkbox name=style_dna value="Elevated Minimalism"><span>Утончённый минимализм</span></label>
   <label class=chip><input type=checkbox name=style_dna value="Quiet Luxury"><span>Тихая роскошь</span></label>
   <label class=chip><input type=checkbox name=style_dna value="Comfort First"><span>Комфорт прежде всего</span></label>
   <label class=chip><input type=checkbox name=style_dna value="Power Tailoring"><span>Властный тейлоринг</span></label>
   <label class=chip><input type=checkbox name=style_dna value="Romantic"><span>Романтика</span></label>
   <label class=chip><input type=checkbox name=style_dna value="Bold & Edgy"><span>Смелость и акцент</span></label>
  </div>
  <label>Какое впечатление должен производить гардероб</label>
  <select name=impression>
   <option value="">— выбери —</option>
   <option value="professional">Уверенность, статус, авторитет</option>
   <option value="effortless">Расслабленность, лёгкость, комфорт</option>
   <option value="creative">Творчество, уникальность, яркость</option>
  </select>
 </div>

 <div class=group><div class=gtitle>3 · Анти-гардероб</div>
  <label>Что ты точно НЕ носишь <span class=sub>— в чём неуютно</span></label>
  <div class=chips>
   <label class=chip><input type=checkbox name=dealbreakers value=clingy><span>Облегающий тонкий трикотаж</span></label>
   <label class=chip><input type=checkbox name=dealbreakers value=fuss><span>Оборки, воланы, лишний декор</span></label>
   <label class=chip><input type=checkbox name=dealbreakers value=rigid><span>Сковывающая движения одежда</span></label>
   <label class=chip><input type=checkbox name=dealbreakers value=loud_prints><span>Слишком пёстрые принты</span></label>
  </div>
 </div>

 <label class=consent style="font-weight:normal;margin-top:8px"><input type=checkbox name=consent_processing required> Согласна на обработку данных согласно <a href="/privacy" target="_blank" rel="noopener">Политике</a>.</label>
 <label class=consent style="font-weight:normal"><input type=checkbox name=consent_transfer required> Согласна на передачу фото в ИИ-сервис для анализа.</label>
</div>
 <button>Узнать вердикт →</button>
 <p class=hint>Профиль стиля заполняется один раз — потом проверка работает мгновенно. Фото не сохраняется.</p>
</form></div>
<script>
(function(){
 var KEY='ssGarmentProfile';
 var f=document.querySelector('form');
 function setVal(name,vals){
  document.querySelectorAll('[name="'+name+'"]').forEach(function(el){
   if(el.type==='checkbox') el.checked = vals.indexOf(el.value)>-1;
   else el.value = vals[0]||'';
  });
 }
 try{
  // профиль из аккаунта (если вошла) важнее локального; иначе — localStorage
  var server={{ profile_json|default('null')|safe }};
  var saved=server||JSON.parse(localStorage.getItem(KEY)||'null');
  if(saved){ Object.keys(saved).forEach(function(k){ setVal(k, saved[k]); });
   var n=document.getElementById('savednote'); if(n) n.style.display='block'; }
 }catch(e){}
 f.addEventListener('submit', function(){
  var data={};
  ['silhouette_lines','fit_focus','impression','fit_challenges','style_dna','dealbreakers'].forEach(function(name){
   var vals=[]; document.querySelectorAll('[name="'+name+'"]').forEach(function(el){
    if(el.type==='checkbox'){ if(el.checked) vals.push(el.value); }
    else if(el.value) vals.push(el.value);
   });
   data[name]=vals;
  });
  try{ localStorage.setItem(KEY, JSON.stringify(data)); }catch(e){}
 });
})();
</script>
</body></html>"""


GARMENT_RESULT = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Вердикт по вещи — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:600px;margin:0 auto;padding:22px 20px 70px}
 .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
 .logo{font-size:18px;letter-spacing:.5px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .vcard{border-radius:18px;padding:34px 26px;text-align:center;color:#fff;margin-top:14px}
 .vicon{font-size:46px;line-height:1;margin-bottom:6px}
 .vlabel{font-size:13px;letter-spacing:.18em;text-transform:uppercase;opacity:.85;font-family:Arial,sans-serif}
 .vword{font-size:38px;font-weight:normal;margin-top:4px}
 .item{font-size:16px;color:var(--muted);margin:20px 0 4px;text-align:center}
 .chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin:18px 0}
 .chip{background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 14px;font-size:13px;color:#5a5246}
 .chip span{color:#9a8f80}
 .reason{font-size:17px;line-height:1.65;margin:18px 0;text-align:center}
 .replace{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 20px;font-size:15px;line-height:1.55}
 .replace b{color:var(--wine)}
 .db{background:#fbf1ef;border:1px solid #eccfc9;border-radius:12px;padding:13px 18px;font-size:14px;color:#9b3030;text-align:center;margin:6px 0}
 .cta{display:block;text-align:center;margin-top:26px;padding:15px;background:var(--wine);color:#fff;border-radius:10px;text-decoration:none;font-size:16px}
 .back{text-align:center;margin-top:14px} .back a{color:var(--wine);font-size:14px}
 .addform{margin-top:22px;text-align:center}
 .addbtn{width:100%;padding:14px;border:1px solid var(--wine);background:#fff;color:var(--wine);
         border-radius:10px;font:inherit;font-size:15px;cursor:pointer}
 .addbtn:hover{background:#fbf6ec}
 .addhint{font-size:13px;color:var(--muted);margin:8px 0 0}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>

<div class=vcard style="background:{{ color }}">
 <div class=vicon>{{ icon }}</div>
 <div class=vlabel>вердикт</div>
 <div class=vword>{{ verdict_ru }}</div>
</div>

{% if item %}<p class=item>На фото: {{ item }}</p>{% endif %}
{% if adds %}<p class=item style="color:var(--wine)">Добавит <b>+{{ adds }}</b> {{ 'образ' if adds == 1 else 'образа' if adds < 5 else 'образов' }} к твоей капсуле.</p>{% endif %}
<div class=chips>
 {% if figure %}<span class=chip><span>линии:</span> {{ figure }}</span>{% endif %}
 {% if style %}<span class=chip><span>стиль:</span> {{ style }}</span>{% endif %}
 {% if palette %}<span class=chip><span>цвет:</span> {{ palette }}</span>{% endif %}
</div>
{% if dealbreaker %}<div class=db>⚠ Сработал твой анти-гардероб: {{ dealbreaker }}</div>{% endif %}
<p class=reason>{{ reason }}</p>
{% if replace_with %}<div class=replace><b>Что проверить / чем заменить:</b> {{ replace_with }}</div>{% endif %}

{# Вердикт без действия — разговор впустую. Если вещь подходит, клиентка должна мочь забрать её
   к себе: дальше гардероб виден в кабинете и участвует в сборке образов. #}
{% if item %}
<form method=post action="/wardrobe/add" class=addform>
 <input type=hidden name=name value="{{ item }}">
 <input type=hidden name=verdict value="{{ verdict_ru }}">
 <input type=hidden name=reason value="{{ reason }}">
 <button type=submit class=addbtn>Добавить в мой гардероб</button>
 <p class=addhint>Вещь появится в кабинете и будет учитываться в образах.</p>
</form>
{% endif %}
<a class=cta href="/garment">Проверить ещё вещь</a>
<div class=back><a href="/cabinet">Мой гардероб →</a></div>
</div></body></html>"""


LOGIN_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Вход — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:460px;margin:0 auto;padding:40px 22px 70px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-size:18px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-family:'Cormorant Garamond',serif;font-weight:normal;font-size:30px;margin:30px 0 8px} .lead{color:var(--muted);margin:0 0 22px}
 .card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:22px}
 label{display:block;font-size:14px;color:var(--muted);margin-bottom:6px}
 input{width:100%;padding:12px;border:1px solid #d9d2c7;border-radius:8px;font:inherit;background:#fff}
 button{margin-top:16px;width:100%;padding:14px;background:var(--wine);color:#fff;border:0;border-radius:10px;font:inherit;font-size:16px;cursor:pointer}
 .ok{background:#eef6ee;border:1px solid #cfe3cf;border-radius:12px;padding:16px;color:#3a5a3a}
 .devlink{margin-top:12px;font-size:13px;word-break:break-all} .devlink a{color:var(--wine)}
 .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:8px;margin-bottom:14px}
 .hint{color:var(--muted);font-size:13px;margin-top:14px}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>
<h1>Вход</h1>
<p class=lead>Введи email — пришлём ссылку для входа. Без пароля. Профиль и Формула сохранятся за тобой.</p>
{% if error %}<p class=err>{{ error }}</p>{% endif %}
{% if sent %}
 <div class=ok>Ссылка для входа отправлена на <b>{{ email }}</b>. Открой письмо и перейди по ссылке (действует 15 минут).</div>
 {% if dev_link %}<div class=devlink>Демо-режим (почта не настроена) — ссылка для входа:<br><a href="{{ dev_link }}">{{ dev_link }}</a></div>{% endif %}
{% else %}
 <form method=post action="/login" class=card>
  <input type=hidden name=next value="{{ next or '' }}">
  <label>Почта</label><input type=email name=email required placeholder="anna@example.com" value="{{ email or '' }}">
  <label style="display:flex;gap:8px;align-items:flex-start;font-size:13px;color:#6b645c;font-weight:normal;margin:12px 0 4px;line-height:1.4"><input type=checkbox name=marketing value=1 style="width:auto;margin-top:3px"> Хочу получать письма о стиле, разборах и новинках (по желанию)</label>
  <button>Получить ссылку для входа</button>
 </form>
 <p class=hint>Входя, ты соглашаешься с <a href="/privacy">Политикой</a>. Пароль не нужен.</p>
{% endif %}
</div></body></html>"""


ME_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Мой профиль — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:860px;margin:0 auto;padding:34px 26px 70px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-size:18px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-family:'Cormorant Garamond',serif;font-weight:normal;font-size:30px;margin:26px 0 4px} .email{color:var(--muted);margin:0 0 22px}
 .card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:14px}
 .card h3{font-weight:normal;font-size:18px;margin:0 0 6px} .card p{margin:0;color:var(--muted);font-size:14px}
 .badge{display:inline-block;font-size:12px;padding:3px 10px;border-radius:999px;margin-left:8px}
 .yes{background:#eef6ee;color:#3a5a3a} .no{background:#f3efe7;color:#9a8f80}
 .links{display:flex;gap:12px;flex-wrap:wrap;margin-top:18px}
 .btn{background:var(--wine);color:#fff;text-decoration:none;padding:12px 18px;border-radius:10px;font-size:15px}
 .btn.sec{background:#fff;color:var(--wine);border:1px solid var(--line)}
 .trackhead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
 .trackhead .sub{color:var(--muted);font-style:italic;font-size:15px}
 .track{margin:14px 0 4px}
 .trow{display:flex;align-items:center;gap:10px;margin:9px 0;font-size:13px}
 .tdate{flex:0 0 128px;color:var(--muted)} .tdate b{color:var(--wine);font-weight:normal}
 .tbar{flex:1;height:10px;background:#efe8db;border-radius:999px;overflow:hidden}
 .tfill{display:block;height:100%;background:var(--wine);border-radius:999px}
 .tval{flex:0 0 40px;text-align:right}
 .tnote{font-size:13px;color:var(--muted);margin:10px 0 14px;line-height:1.5}
 .tdelta{background:#eef6ee;color:#3a5a3a;font-size:12px;padding:3px 10px;border-radius:999px;white-space:nowrap}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span>{% if email %}<a href="/logout">Выйти</a>{% else %}<a href="/">← на главную</a>{% endif %}</div>
<h1>Мой профиль</h1>
{% if email %}<p class=email>{{ email }}</p>
{% else %}<p class=email>Результат сохранён на этом устройстве. Чтобы он не потерялся при смене браузера — <a href="/login">привяжи почту</a>.</p>{% endif %}
<div class=card><h3>Формула стиля {% if has_diag %}<span class="badge yes">есть</span>{% else %}<span class="badge no">ещё нет</span>{% endif %}</h3>
 <p>{% if has_diag %}{{ formula }}{% else %}Пройди диагностику — Формула сохранится здесь.{% endif %}</p></div>
{% if track %}
<div class=card>
 <div class=trackhead><h3>Эволюция <span class=sub>индекса настройки образа</span></h3>
  {% if track.delta is not none and track.delta > 0 %}<span class=tdelta>−{{ track.delta }} п.п.</span>{% endif %}</div>
 <div class=track>
  {% for p in track.points %}
  <div class=trow>
   <span class=tdate>{{ p.date }}{% if loop.first %} · <b>точка отсчёта</b>{% endif %}</span>
   <span class=tbar><span class=tfill style="width:{{ p.gap }}%"></span></span>
   <span class=tval>{{ p.gap }}%</span>
  </div>
  {% endfor %}
 </div>
 {% if track.measurements < 2 %}
 <p class=tnote>Это твоя точка отсчёта. Сделай пере-замер через время — увидишь, как меняется индекс настройки образа. Он двигается только от настоящего замера: новых фото того, как ты одеваешься сейчас.</p>
 {% else %}
 <p class=tnote>Разрыв закрывается — и это видно. Двигается он только от реального пере-замера, поэтому цифре можно верить.</p>
 {% endif %}
 <a class="btn sec" href="/identity-scan-quiz.html?fresh=1" style="display:inline-block">Сделать пере-замер</a>
</div>
{% endif %}
<div class=card><h3>Профиль «Примерочной» {% if has_style %}<span class="badge yes">заполнен</span>{% else %}<span class="badge no">не заполнен</span>{% endif %}</h3>
 <p>Линии, ДНК стиля и анти-гардероб — чтобы проверка вещей работала мгновенно.</p></div>
<div class=links>
 {% if has_diag %}<a class=btn href="/card">Открыть Карту стиля</a>{% else %}<a class=btn href="/identity-scan-quiz.html?fresh=1">Пройти диагностику</a>{% endif %}
 {% if has_diag %}<a class="btn sec" href="/cabinet">Мой гардероб</a>{% endif %}
 <a class="btn sec" href="/garment">Проверить вещь</a>
 <a class="btn sec" href="/stylist">Спросить стилиста</a>
</div>
</div></body></html>"""


STYLE_CARD = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Карта стиля{% if name %} — {{ name }}{% endif %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 /* ── Карта стиля: дашборд по макету фаундера от 19.07.2026 ────────────────────────────────
    Карта — не длинная статья, а рабочая панель: слева постоянная навигация и тариф, справа
    две колонки. Левая колонка — «что носить» (образы, покупки, палитра), правая — «на чём это
    держится» (капсула-ядро, сочетания, стоп-лист). Всё, что раньше растягивалось на десяток
    экранов, теперь либо в панели, либо свёрнуто в разделы «Разбор» внизу. */
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--wine2:#7A2438;--muted:#6b645c;
       --line:#e6dfd2;--sand:#F3ECDF;--soft:#FBF6EC}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;margin:0;
      background:var(--cream);color:var(--ink);line-height:1.55;-webkit-font-smoothing:antialiased}
 a{color:inherit}
 .serif{font-family:'Cormorant Garamond',Georgia,serif}
 .shell{display:grid;grid-template-columns:228px 1fr;min-height:100vh}

 /* ── левая колонка ───────────────────────────────────────────────────────────────────── */
 .side{background:var(--sand);border-right:1px solid var(--line);padding:24px 16px 22px;
       display:flex;flex-direction:column;gap:22px;position:sticky;top:0;align-self:start;height:100vh}
 .sidelogo{font-family:'Cormorant Garamond',Georgia,serif;font-size:23px;line-height:1.12;padding:0 8px}
 .sidelogo span{display:block;font-family:Onest,sans-serif;font-size:9px;letter-spacing:.17em;
                text-transform:uppercase;color:var(--muted);margin-top:5px;font-weight:400}
 .sidenav{display:flex;flex-direction:column;gap:2px}
 .sidenav a{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:11px;
            color:#4e473f;text-decoration:none;font-size:14px;transition:background .12s,color .12s}
 .sidenav a svg{flex:0 0 auto;width:17px;height:17px;stroke:currentColor;fill:none;
                stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round;opacity:.75}
 .sidenav a:hover{background:rgba(255,255,255,.65)}
 .sidenav a.on{background:var(--wine);color:#fff}
 .sidenav a.on svg{opacity:1}
 .sidetariff{margin-top:auto;background:#fff;border:1px solid var(--line);border-radius:16px;padding:15px 16px}
 .st-k{font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
 .st-n{font-family:'Cormorant Garamond',Georgia,serif;font-size:19px;line-height:1.15;color:var(--wine);margin:5px 0 4px}
 .st-d{font-size:12px;color:var(--muted);line-height:1.4}
 .st-d b{color:#4e473f;font-weight:500}
 .sidetariff a{display:block;margin-top:12px;text-align:center;padding:9px;border:1px solid var(--line);
               border-radius:10px;color:var(--wine);text-decoration:none;font-size:12.5px}
 .sidetariff a:hover{border-color:var(--wine)}

 /* ── рабочая область ─────────────────────────────────────────────────────────────────── */
 /* max-width: на мониторе 2560px дашборд растягивался во всю ширину — строки становились
    нечитаемо длинными, карточки неоправданно огромными. Ограничиваем и центруем. */
 .main{padding:22px 26px 60px;min-width:0;max-width:1560px;margin:0 auto;width:100%}
 .panel{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px 20px;min-width:0}
 .ph{font-family:'Cormorant Garamond',Georgia,serif;font-size:22px;line-height:1.15;margin:0;
     display:flex;align-items:center;gap:8px;flex-wrap:wrap}
 .ph .dot{width:15px;height:15px;flex:0 0 auto;opacity:.5}
 .ph .more{margin-left:auto;font-family:Onest,sans-serif;font-size:12px;color:var(--wine);
           text-decoration:none;font-weight:400;white-space:nowrap}
 .psub{font-size:12px;color:var(--muted);margin:4px 0 0;line-height:1.45}

 /* шапка профиля */
 .profbar{display:flex;align-items:center;gap:15px;background:#fff;border:1px solid var(--line);
          border-radius:18px;padding:14px 18px;margin-bottom:16px;flex-wrap:wrap}
 .profav{width:54px;height:54px;border-radius:50%;background:var(--wine);color:#fff;display:flex;
         align-items:center;justify-content:center;font-family:'Cormorant Garamond',serif;
         font-size:24px;flex:0 0 auto;overflow:hidden}
 .profav img{width:100%;height:100%;object-fit:cover}
 .profwho{min-width:0;flex:1 1 220px}
 .profname{font-family:'Cormorant Garamond',Georgia,serif;font-size:26px;line-height:1.1;
           overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .proff{font-size:14px;color:var(--muted)}
 .proff b{color:var(--wine2);font-weight:400;font-family:'Cormorant Garamond',Georgia,serif;font-size:16px}
 .profchips{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
 .profchip{display:flex;align-items:center;gap:9px;background:var(--soft);border:1px solid var(--line);
           border-radius:13px;padding:7px 15px;max-width:250px}
 .profchip .pi{font-size:15px;line-height:1}
 .profchip > span:last-child{min-width:0}
 .profchip .pk{font-size:11px;color:var(--muted);display:block}
 /* значение чипа — одна строка с многоточием: длинный цветотип/фигура иначе рвал шапку */
 .profchip .pv{font-size:13.5px;color:var(--ink);display:block;font-weight:400;
               overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .profedit{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:11px;
           padding:10px 15px;font-size:13px;color:#4e473f;text-decoration:none;background:#fff}
 .profedit:hover{border-color:var(--wine);color:var(--wine)}

 /* верхний ряд из трёх карточек */
 .toprow{display:grid;grid-template-columns:.95fr 1.1fr .95fr;gap:15px;margin-bottom:16px;align-items:stretch}
 /* Формула из трёх частей длиннее двухсоставной — размер уменьшаем через clamp по ширине
    панели, чтобы «Smart Casual × Драма-акцент × Классическая сдержанность» не разъезжалась. */
 /* Формула — главное в карточке: направление тёмным, уточнение винным с новой строки.
    Знак × приглушён до разделителя, чтобы не спорил с самими словами. */
 .dnaformula{font-family:'Cormorant Garamond',Georgia,serif;font-size:clamp(22px,2.3vw,30px);
             line-height:1.16;margin:16px 0 12px;overflow-wrap:break-word;letter-spacing:-.015em}
 /* × уходит в начало второй строки, а не висит в конце первой */
 .dnaformula .x{color:#c9bda9;font-weight:400;margin-right:8px;font-size:.8em;vertical-align:.06em}
 .dnaformula .b{color:var(--wine2);display:block;margin-top:1px}
 .dnarule{height:1px;margin:16px 0 0;opacity:.32;
          background:linear-gradient(90deg,var(--wine),rgba(93,34,48,0))}
 .dnak{font-size:9.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin:15px 0 7px}
 /* ДНК в долях. Полоса тонкая и без обводок: это акцент, а не диаграмма — карточка
    должна оставаться про формулу. Кружки в легенде убраны, цвет уже в полосе. */
 .dnabar{display:flex;height:6px;border-radius:999px;overflow:hidden;background:var(--sand)}
 .dnabar span{display:block;height:100%}
 .dnalegend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:9px}
 .dnaleg{font-size:11.5px;color:var(--muted);letter-spacing:.01em}
 .dnaleg b{color:var(--ink);font-weight:500;margin-left:2px}
 .subchips{display:flex;gap:7px;flex-wrap:wrap}
 .subchip{border:1px solid var(--line);border-radius:999px;padding:5px 13px;font-size:12.5px;background:var(--soft)}
 /* Черты — списком с тонкими разделителями, а не сеткой 2×2: в узкой колонке сетка
    ломала длинные формулировки на обрывки и карточка выглядела тесной. */
 .traits{display:flex;flex-direction:column;font-size:12.5px;color:#4e473f}
 .traits div{display:flex;align-items:flex-start;gap:8px;line-height:1.35;min-width:0;
             padding:8px 0;border-bottom:1px solid var(--line)}
 .traits div:last-child{border-bottom:0;padding-bottom:0}
 .traits i{flex:0 0 auto;color:var(--wine2);font-style:normal;font-size:10px;margin-top:2px}
 /* черта в две строки максимум: длинные формулировки («жакеты с деликатно обозначенным…»)
    иначе разгоняли карточку по высоте и рвали ряд из трёх панелей */
 .traits span{min-width:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .idxwrap{display:flex;align-items:center;gap:18px;margin-top:10px}
 .idxring{width:118px;height:118px;flex:0 0 auto}
 .idxnum{font-family:'Cormorant Garamond',Georgia,serif;font-size:30px;fill:var(--wine)}
 .idxtext{font-size:12.5px;color:#4e473f;line-height:1.5;margin:0}
 .idxtext + .idxtext{margin-top:9px;color:var(--muted)}
 .idxmore{display:inline-block;margin-top:11px;font-size:12.5px;color:var(--wine);text-decoration:none}
 .effchips{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
 .effchip{border:1px solid var(--line);border-radius:999px;padding:6px 15px;font-size:13px;background:var(--soft)}

 /* две рабочие колонки */
 .cols{display:grid;grid-template-columns:1.42fr 1fr;gap:16px;align-items:start}
 .col{display:flex;flex-direction:column;gap:16px;min-width:0}

 /* образы под роли жизни */
 .secttl{font-family:'Cormorant Garamond',Georgia,serif;font-size:25px;margin:0 0 11px;line-height:1.1}
 .looksgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
 .lookcard{display:grid;grid-template-columns:132px minmax(0,1fr);background:#fff;border:1px solid var(--line);
           border-radius:18px;overflow:hidden;text-decoration:none;color:inherit;min-height:182px}
 .lookcard:hover{border-color:#d5c9b6;box-shadow:0 8px 22px rgba(40,26,20,.06)}
 .lookpic{background:var(--sand);width:100%;height:100%;object-fit:cover;display:block}
 .lookpic.empty{display:block;background:var(--sand)}
 .lookbody{padding:14px 15px 13px;display:flex;flex-direction:column;min-width:0}
 .lookttl{display:flex;align-items:flex-start;gap:8px;font-size:14px;font-weight:500;color:var(--ink);min-width:0}
 .lookttl .lt{min-width:0;flex:1 1 auto;font-family:'Cormorant Garamond',Georgia,serif;font-size:20px;line-height:1.02;letter-spacing:-.01em;
              display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .lookttl .chev{margin-left:auto;color:var(--muted);font-size:16px;line-height:1.1;padding-top:2px;flex:0 0 auto}
 .lookdesc{font-size:13px;color:var(--muted);line-height:1.45;margin:9px 0 0;
           display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
 .lookmatch{margin-top:auto;padding-top:12px;text-align:left;font-size:11px;color:var(--muted);letter-spacing:.08em;text-transform:uppercase}
 .lookmatch b{display:block;margin-top:3px;color:var(--wine);font-weight:500;font-size:18px;letter-spacing:-.01em;text-transform:none}
 .noimg{background:var(--soft);border:1px solid var(--line);border-left:3px solid var(--wine);
        border-radius:12px;padding:14px 16px;margin:0 0 12px;font-size:13.5px}
 .noimg a{display:inline-block;margin-top:8px;background:var(--wine);color:#fff;text-decoration:none;
          padding:9px 16px;border-radius:8px;font-size:13px}

 /* нижняя пара левой колонки: покупки + палитра */
 .buyrow{display:grid;grid-template-columns:1.55fr 1fr;gap:16px;align-items:start}
 .buygrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:13px}
 .buycard{border:1px solid var(--line);border-radius:12px;padding:11px 12px;background:var(--soft);
          display:flex;flex-direction:column;min-width:0}
 /* Названия из генерации длинные («жакет полуприлегающего силуэта серо-синего цвета»).
    Заголовок — до двух строк, обоснование — до четырёх: карточки в ряду одной высоты,
    и текст обрывается многоточием, а не на полуслове посреди строки. */
 .buyimg{width:100%;aspect-ratio:1/1;object-fit:contain;border-radius:9px;background:#F7F2E9;
         border:1px solid var(--line);margin-bottom:9px;display:block}
 .buyname{font-size:13px;font-weight:500;line-height:1.3;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .buywhy{font-size:11.5px;color:var(--muted);line-height:1.4;margin:8px 0 0;
         display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
 .buywhy b{color:var(--wine);font-weight:500}
 .buylinks{margin-top:auto;padding-top:9px;font-size:11px;color:var(--muted)}
 .buylinks a{color:var(--wine);text-decoration:none}
 .palgrp{font-size:10px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin:14px 0 7px}
 .palrow{display:flex;flex-wrap:wrap;gap:7px}
 .palrow .c{width:26px;height:26px;border-radius:50%;border:1px solid rgba(0,0,0,.09)}

 /* капсула-ядро */
 .capgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;margin-top:12px}
 .capcard{border:1px solid var(--line);border-radius:13px;overflow:hidden;background:#fff;
          text-decoration:none;color:inherit;display:flex;flex-direction:column;position:relative}
 .capcard:hover{border-color:#d5c9b6}
 /* contain, а не cover: у вещей каталога кадр вертикальный, cover срезал верх и низ —
    от плаща оставался кусок полы. Фон чуть темнее белого, иначе белая блуза сливалась в пустоту. */
 .capimg{width:100%;aspect-ratio:1/1;object-fit:contain;display:block;background:#F7F2E9;
         border-bottom:1px solid var(--line)}
 /* нет фото — не одинокая буква на пустом поле, а подпись, по которой понятно, что это за вещь */
 .capimg.empty{display:flex;align-items:center;justify-content:center;text-align:center;padding:10px;
               color:#8d8175;font-size:11px;line-height:1.3;background:var(--soft)}
 .capbadge{position:absolute;top:7px;right:7px;background:rgba(255,255,255,.94);border:1px solid var(--line);
           border-radius:999px;padding:3px 9px;font-size:9.5px;color:var(--wine);letter-spacing:.02em}
 .capbody{padding:9px 10px 11px;min-width:0}
 /* название вещи из генерации бывает длинным («кожаный плащ миди цвета мягкого какао») —
    держим три строки, дальше многоточие, иначе карточки в ряду разной высоты */
 .capname{font-size:12px;line-height:1.3;color:var(--ink);
          display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
 .capexample{font-size:9.5px;color:#9a9086;margin-top:3px;font-style:italic}
 .capbrand{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-top:3px}
 .capprice{font-size:11.5px;color:#4e473f;margin-top:3px}
.capfind{font-size:10.5px;color:var(--muted);margin-top:5px}
.capfind a{color:var(--wine);text-decoration:none}
.caporigin{display:flex;align-items:flex-start;gap:10px;margin-top:12px;padding:11px 12px;border:1px solid rgba(93,34,48,.12);
           border-radius:12px;background:linear-gradient(135deg,#fbf6ec,#fff)}
.caporigin i{flex:0 0 auto;width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;
             background:var(--wine);color:#fff;font-style:normal;font-size:12px;line-height:1}
.caporigin b{display:block;font-size:12.5px;color:var(--ink);margin-bottom:2px}
.caporigin span{display:block;font-size:11.5px;color:var(--muted);line-height:1.35}
.caporigin a{color:var(--wine);text-decoration:none}
.capscen{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.capscen span{display:inline-flex;align-items:center;min-height:22px;padding:0 8px;border-radius:999px;background:var(--soft);
              border:1px solid rgba(93,34,48,.10);font-size:10.5px;color:#5b5249;line-height:1.2}

 /* лента сочетаний */
 /* Матрица «база × слой»: одна база — несколько ролей. */
 .matrix{margin:14px 0 6px;border:1px solid rgba(93,34,48,.12);border-radius:13px;overflow:hidden}
 .matrixhead{padding:10px 13px;background:linear-gradient(135deg,#fbf6ec,#fff);font-size:13px;
             border-bottom:1px solid rgba(93,34,48,.10)}
 .matrixhead span{display:block;font-size:11px;color:var(--muted);margin-top:2px}
 .matrixrow{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1.4fr);gap:10px;
            padding:9px 13px;border-bottom:1px solid var(--line)}
 .matrixrow:last-child{border-bottom:0}
 .matrixbase{font-size:12px;color:var(--ink);line-height:1.35}
 .matrixcells{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:7px}
 .matrixcell{background:var(--soft);border-radius:9px;padding:7px 9px}
 .mcrole{display:block;font-size:11.5px;color:var(--wine);font-weight:500}
 .mcwhy{display:block;font-size:10.5px;color:var(--muted);line-height:1.3;margin-top:2px}
 @media(max-width:560px){.matrixrow{grid-template-columns:1fr}}
 .econ{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0 4px}
 .econcell{border:1px solid rgba(93,34,48,.12);border-radius:12px;padding:11px 13px;
           background:linear-gradient(135deg,#fbf6ec,#fff)}
 .econcell b{display:block;font-family:'Cormorant Garamond',Georgia,serif;font-size:24px;color:var(--wine);line-height:1}
 .econcell span{display:block;font-size:11px;color:var(--muted);margin-top:5px;line-height:1.35}
 .combolane{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}
 .combo{border:1px solid var(--line);border-radius:12px;padding:8px;background:var(--soft);min-width:0}
 .combopics{display:flex;gap:4px}
 /* flex:1 1 0 + min-width:0 обязательны: при width:100% на каждой из трёх картинок они
    суммарно втрое шире плитки и вылезали наружу вместе с буквами-заглушками. */
 .combopics img{flex:1 1 0;min-width:0;width:100%;height:52px;object-fit:cover;
                border-radius:5px;background:var(--sand)}
 .combodot{flex:1 1 0;min-width:0;height:52px;border-radius:5px;background:var(--sand);display:flex;
           align-items:center;justify-content:center;color:var(--wine);
           font-family:'Cormorant Garamond',serif;font-size:15px}
 .combotitle{font-family:'Cormorant Garamond',Georgia,serif;font-size:18px;line-height:1.08;margin-top:8px;color:var(--ink)}
 .combodesc{font-size:11.5px;color:var(--muted);line-height:1.35;margin-top:5px;
            display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}

 /* что уводит от формулы */
 .stopgrid{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-top:12px}
 .stopcell{position:relative;text-align:center}
 .stopbox{aspect-ratio:1/1;border:1px solid var(--line);border-radius:10px;background:var(--sand);
          display:flex;align-items:center;justify-content:center;overflow:hidden}
 .stopbox img{width:100%;height:100%;object-fit:cover;filter:grayscale(.55) opacity(.75)}
 .stopbox .sw{width:100%;height:100%}
 .stopbox.text{padding:8px;background:var(--soft)}
 .stopbox.text span{font-size:10.5px;color:#7a6f64;line-height:1.3;text-align:center;
                    display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
 .stopx{position:absolute;top:5px;right:5px;width:17px;height:17px;border-radius:50%;background:#B23A38;
        color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;
        border:1.5px solid #fff;line-height:1}
 .stopcap{font-size:10px;color:var(--muted);margin-top:6px;line-height:1.25;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

 /* нижняя лента */
 .footband{display:flex;align-items:center;gap:20px;background:linear-gradient(135deg,#fff,#fbf6ec);
           border:1px solid var(--line);border-radius:18px;padding:20px 24px;margin-top:18px;flex-wrap:wrap}
 .mono{flex:0 0 auto;width:54px;height:54px;border-radius:50%;border:1px solid var(--line);
       background:var(--soft);color:var(--wine);font-family:'Cormorant Garamond',serif;font-size:23px;
       display:flex;align-items:center;justify-content:center;letter-spacing:.02em}
 .foottext{font-size:14px;color:#4e473f;line-height:1.5;min-width:200px}
 .footbtns{margin-left:auto;display:flex;gap:12px;flex-wrap:wrap}
 .btnfill{background:var(--wine);color:#fff;border:1px solid var(--wine);border-radius:11px;
          padding:13px 26px;font:inherit;font-size:14px;cursor:pointer;text-decoration:none;
          display:inline-flex;align-items:center;gap:9px}
 .btnline{background:#fff;color:var(--wine);border:1px solid var(--wine);border-radius:11px;
          padding:13px 26px;font-size:14px;text-decoration:none;display:inline-flex;align-items:center;gap:9px}

 /* блок постоянной ссылки на Карту */
 .sharebox{display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap;
           background:#fff;border:1px solid var(--line);border-radius:18px;padding:16px 20px;margin-top:16px}
 .sharek{font-family:'Cormorant Garamond',Georgia,serif;font-size:19px;line-height:1.2}
 .sharep{font-size:12.5px;color:var(--muted);margin:4px 0 0;max-width:430px;line-height:1.45}
 .sharerow{display:flex;gap:8px;align-items:center;flex:1 1 320px;min-width:0}
 .shareinput{flex:1 1 auto;min-width:0;padding:10px 12px;border:1px solid var(--line);border-radius:10px;
             font:inherit;font-size:12.5px;background:var(--soft);color:#4e473f;text-overflow:ellipsis}
 .sharebtn{flex:0 0 auto;padding:10px 18px;border:1px solid var(--wine);border-radius:10px;
           background:var(--wine);color:#fff;font:inherit;font-size:13px;cursor:pointer}
 .sharebtn.done{background:#3a6b46;border-color:#3a6b46}
 @media print{.sharebox{display:none!important}}
 /* разделы «разбор» — глубина, которая не помещается в панель */
 .deep{margin-top:26px}
 /* Раскладка образа: он на клиентке + flat-lay вещей, из которых собран. */
 /* Пара «одна вещь — два образа»: слева она в образе, справа раскладка его вещей. */
 .pairrow{display:grid;grid-template-columns:minmax(0,240px) minmax(0,1fr);gap:14px;align-items:start;margin-bottom:10px}
 .pairmodel{width:100%;border-radius:12px;display:block}
 .pairflat{width:100%;border-radius:12px;display:block;background:#faf6ee}
 .pairitems{font-size:12px;color:var(--muted);margin:0 0 8px}
 @media(max-width:620px){.pairrow{grid-template-columns:1fr}}
 .lookflat{display:grid;grid-template-columns:minmax(0,200px) 1fr;gap:16px;align-items:start}
 .lookflat-noimg{grid-template-columns:1fr}
 .lookmodel img{width:100%;border-radius:12px;display:block}
 .lookpieces{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:10px}
 .lookpiece{display:flex;flex-direction:column;gap:5px;text-align:center}
 .lookpiece img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:9px;border:1px solid var(--line);background:#faf6ee}
 .lookpiece-ph i{display:block;font-style:normal;font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:#a89c8c;margin-bottom:5px}
 .lookpiece-ph b{display:block;font-weight:400;font-size:10.5px;color:#6b645c;line-height:1.3}
 .lookpiece-ph{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:8px;aspect-ratio:3/4;border-radius:9px;
               border:1px dashed #cdbfa6;background:var(--soft);font-size:10px;color:var(--muted)}
 .lookpiece-nm{font-size:10.5px;color:#5b5249;line-height:1.3}
 .lookmatchsm{font-family:Onest,sans-serif;font-size:11px;font-weight:400;color:var(--wine);margin-left:10px;letter-spacing:.04em}
 @media(max-width:560px){.lookflat{grid-template-columns:1fr}.lookmodel img{max-width:220px}}
 .deep summary{cursor:pointer;list-style:none;font-family:'Cormorant Garamond',Georgia,serif;
               font-size:23px;padding:14px 20px;background:#fff;border:1px solid var(--line);
               border-radius:14px;display:flex;align-items:center;gap:10px}
 .deep summary::-webkit-details-marker{display:none}
 .deep summary::after{content:'+';margin-left:auto;color:var(--wine);font-size:20px;font-family:Onest,sans-serif}
 .deep[open] summary::after{content:'–'}
 .deep summary span{font-family:Onest,sans-serif;font-size:12.5px;color:var(--muted);font-weight:300}
 .deepbody{padding:16px 20px 4px}
 .deepbody h3{font-family:'Cormorant Garamond',Georgia,serif;font-size:20px;margin:18px 0 8px}
 .deepbody h3:first-child{margin-top:0}
 .deepbody p{font-size:14px;color:#4a443c;margin:0 0 10px}
 ul.clean{list-style:none;padding:0;margin:0 0 10px}
 ul.clean li{padding:5px 0 5px 17px;position:relative;font-size:14px}
 ul.clean li:before{content:'·';position:absolute;left:4px;color:var(--wine)}
 ul.clean.stop li:before{content:'✕';font-size:10px;color:#B23A38}
 .swatches{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px}
 .sw{width:82px} .sw .chip{height:48px;border-radius:8px;border:1px solid rgba(0,0,0,.08)}
 .sw .nm{font-size:11px;color:#4a443c;margin-top:4px;line-height:1.25}
 .sw .nm b{color:#B23A38;font-weight:500}
 .shopfull{display:flex;flex-direction:column;gap:9px}
 .shopfull .si{border:1px solid var(--line);border-radius:11px;padding:12px 15px;background:#fff}
 .shopfull .sn{font-size:15px} .shopfull .sy{font-size:13px;color:var(--muted);margin:3px 0 5px}
 .shopfull .sl{font-size:12px;color:var(--muted)} .shopfull .sl a{color:var(--wine);text-decoration:none}
 .ref{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:14px 18px}
 .refname{font-size:19px;color:var(--wine);font-family:'Cormorant Garamond',serif}
 .refline{font-size:13.5px;color:#5a5246;margin:5px 0 0}

 .stale{background:#fbeee4;border:1px solid #e3cdb8;border-radius:14px;padding:14px 18px;margin:0 0 16px;
        font-size:14px;color:#5a4a3a;line-height:1.5}
 .stale b{color:var(--wine)}
 .stale a{display:inline-block;margin-top:9px;background:var(--wine);color:#fff;text-decoration:none;
          padding:9px 18px;border-radius:8px;font-size:13.5px}
 .fbblock{margin-top:20px;padding:20px 22px;border:1px solid var(--line);border-radius:16px;background:#fff}
 .fbblock h2{font-family:'Cormorant Garamond',Georgia,serif;font-size:23px;margin:0 0 4px}
 .fbblock p.h{margin:0 0 14px;color:var(--muted);font-size:13.5px}
 .fbblock textarea{width:100%;padding:11px 13px;border:1px solid #d9d2c7;border-radius:10px;
                   font:inherit;font-size:14.5px;resize:vertical}
 .fbblock button{margin-top:12px;padding:12px 24px;background:var(--wine);color:#fff;border:0;
                 border-radius:10px;font:inherit;font-size:14.5px;cursor:pointer}

 /* ── адаптив ─────────────────────────────────────────────────────────────────────────── */
 @media(min-width:1480px){.looksgrid{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media(max-width:1280px){.cols{grid-template-columns:1.2fr 1fr}.combolane{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media(max-width:1080px){
  .shell{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:14px}
  .sidenav{flex-direction:row;flex-wrap:wrap}
  .sidetariff{margin:0;flex:1 1 240px}
  .toprow,.cols,.buyrow{grid-template-columns:1fr}
  .profchips{margin-left:0;width:100%}
 }
 @media(max-width:760px){
  .main{padding:18px 16px 50px}
  .looksgrid,.capgrid,.buygrid{grid-template-columns:1fr 1fr}
  .stopgrid{grid-template-columns:repeat(3,1fr)}
  .footbtns{margin-left:0;width:100%}
  .btnfill,.btnline{flex:1;justify-content:center}
 }
 @media(max-width:640px){.lookcard{grid-template-columns:1fr}.lookpic{aspect-ratio:4/4.8;height:auto}.lookbody{padding:13px 14px 14px}}
 @media(max-width:460px){.looksgrid,.capgrid,.buygrid{grid-template-columns:1fr}}

 /* ── печать: дашборд разворачиваем в один поток ──────────────────────────────────────── */
 @media print{
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .side,.footband,.fbblock,.profedit,.ph .more,.idxmore{display:none!important}
  .shell{display:block} .main{padding:0}
  .cols,.buyrow,.toprow{display:block}
  .col > *,.panel{margin-bottom:12px}
  .looksgrid{grid-template-columns:1fr 1fr}
  .capgrid{grid-template-columns:repeat(3,1fr)}
  .deep{display:block} .deep summary{display:none} .deepbody{padding:0}
  .lookcard,.capcard,.buycard,.combo,.sw,.ref{break-inside:avoid;page-break-inside:avoid}
  .lookpic{max-height:6cm}
  .panel{break-inside:avoid;page-break-inside:avoid}
 }
</style></head><body>
{# Формула вида «Леди-лайк × Soft Classic»: первая часть — ведущий стиль, вторая — подстиль.
   Разводим их по цвету, как в макете: направление тёмное, уточнение — винным. #}
{# Формула бывает и из трёх частей («Smart Casual × Драма-акцент × Классическая сдержанность»):
   берём ВСЁ после первого ×, иначе третья часть молча пропадала с экрана. #}
{% set fparts = (c.formula or '').split('×') %}
{% set flead = fparts[0]|trim %}
{% set fmore = fparts[1:]|map('trim')|select|list %}
{% set idx = (100 - c.gap) if c.get('gap') is not none else none %}
<div class=shell>
<div class=side>
 <div class=sidelogo>Чувство стиля<span>твоя формула стиля</span></div>
 <nav class=sidenav>
  <a class=on href="#top"><svg viewBox="0 0 20 20"><rect x="2.5" y="3.5" width="15" height="13" rx="2"/><path d="M2.5 8h15M8 8v8.5"/></svg>Моя карта стиля</a>
  <a href="/cabinet"><svg viewBox="0 0 20 20"><rect x="2.5" y="4" width="15" height="13" rx="2"/><path d="M2.5 8h15M6.5 2.5v3M13.5 2.5v3"/></svg>Стиль каждый день</a>
  <a href="#capsule"><svg viewBox="0 0 20 20"><rect x="2.5" y="2.5" width="6" height="6" rx="1.4"/><rect x="11.5" y="2.5" width="6" height="6" rx="1.4"/><rect x="2.5" y="11.5" width="6" height="6" rx="1.4"/><rect x="11.5" y="11.5" width="6" height="6" rx="1.4"/></svg>Капсула</a>
  <a href="#looks"><svg viewBox="0 0 20 20"><rect x="3" y="2.5" width="14" height="15" rx="2"/><circle cx="10" cy="7.5" r="2.2"/><path d="M5.5 16c1-2.6 2.6-4 4.5-4s3.5 1.4 4.5 4"/></svg>Образы и роли</a>
  <a href="#shopping"><svg viewBox="0 0 20 20"><path d="M4 6.5h12l-1 10.5H5z"/><path d="M7.2 6.5V5a2.8 2.8 0 0 1 5.6 0v1.5"/></svg>Покупки</a>
  <a href="/cabinet#wardrobe"><svg viewBox="0 0 20 20"><path d="M10 4.5a1.6 1.6 0 1 1 1.6 1.6c0 1-1.6 1.2-1.6 2.4"/><path d="M2.5 14.5 10 8.5l7.5 6v2h-15z"/></svg>Конструктор</a>
  <a href="/cabinet#week"><svg viewBox="0 0 20 20"><rect x="2.5" y="4" width="15" height="13" rx="2"/><path d="M2.5 8h15M6.5 2.5v3M13.5 2.5v3"/></svg>План недели</a>
 </nav>
 <div class=sidetariff>
  <div class=st-k>Тариф</div>
  <div class=st-n>Карта стиля</div>
  <div class=st-d>Разовый персональный результат{% if c.season_label %}<br><b>{{ c.season_label }}</b>{% endif %}</div>
  {% if not shared %}<a href="/card?rebuild=1">Собрать заново</a>{% endif %}
 </div>
</div>

<div class=main id=top>

{# ── шапка профиля ───────────────────────────────────────────────────────────────────── #}
<div class=profbar>
 <div class=profav>{{ (name or 'К')[0]|upper }}</div>
 <div class=profwho>
  <div class=profname>{{ name or 'Твоя Карта' }}</div>
  <div class=proff><b>{{ c.formula }}</b></div>
 </div>
 <div class=profchips>
  {% if c.season_label %}<div class=profchip><span class=pi>❦</span><span><span class=pk>Сезон</span><span class=pv>{{ c.season_label }}</span></span></div>{% endif %}
  {% if c.colortype %}<div class=profchip><span class=pi>✦</span><span><span class=pk>Цветотип</span><span class=pv>{{ c.colortype }}</span></span></div>{% endif %}
  {# Короткое имя фигуры: длинное описание («Выраженная талия, сбалансированные пропорции»)
     растягивало чип на половину шапки. Полное — в подсказке. #}
  {% if figure_short or c.figure %}<div class=profchip title="{{ c.figure }}"><span class=pi>◈</span><span><span class=pk>Фигура</span><span class=pv>{{ figure_short or c.figure }}</span></span></div>{% endif %}
  <a class=profedit href="/me">Редактировать профиль <span>✎</span></a>
 </div>
</div>

{# Карта собрана без модели (кончились кредиты или выключена генерация). Молчать об этом
   нельзя: клиентка должна понимать, почему нет образов и текстов. #}
{% if c.no_generation %}<div class=stale><b>Это черновик Карты — без лукбука образов.</b> Формула, индекс настройки образа, палитра, силуэты и опорная капсула уже здесь. Визуальные сценарии, тексты и лист умных покупок появятся, когда сборка снова будет доступна.<br><a href="/card?rebuild=1">Собрать с образами →</a></div>{% endif %}

{% if stale %}<div class=stale><b>Твоя диагностика обновилась.</b> Ты недавно заново прошла квиз, и индекс настройки образа изменился. Эта Карта собрана на прежней диагностике — числа и подборка ниже от неё.<br><a href="/card?rebuild=1">Собрать Карту заново →</a></div>{% endif %}

{# ── верхний ряд: ДНК · индекс · желаемый эффект ─────────────────────────────────────── #}
<div class=toprow>

 <div class=panel>
  <h2 class=ph>Твоя ДНК стиля</h2>
  <div class=dnaformula>{{ flead }}{% for p in fmore %}<span class=b><span class=x>&times;</span>{{ p }}</span>{% endfor %}</div>
  {% if c.substyles %}
  {# Подстиль приходит машинным кодом (smart_casual) — на экране клиентки это выглядит как
     утечка внутренностей. Подчёркивания в пробелы, первая буква заглавная. #}
  <div class=subchips>{% for sub in c.substyles %}<span class=subchip>{{ sub|replace('_',' ')|capitalize }}</span>{% endfor %}</div>
  {% endif %}
  {# Доли четырёх полей метода — это и есть результат ДНК-теста: формула называет направление,
     а полоса показывает, из чего оно собрано. #}
  {% if dna_fields %}
  <div class=dnak>Из чего собран твой стиль</div>
  <div class=dnabar>
   {% for f in dna_fields %}<span style="width:{{ f.pct }}%;background:{{ f.hex }}" title="{{ f.label }} · {{ f.pct }}%"></span>{% endfor %}
  </div>
  <div class=dnalegend>
   {% for f in dna_fields %}<span class=dnaleg>{{ f.label }} <b>{{ f.pct }}%</b></span>{% endfor %}
  </div>
  {% endif %}
  {# Ключевые черты — визуальные коды формулы: что именно делает образ её, а не просто название
     направления. Желаемый эффект («как хочу считываться») живёт в третьей карточке. #}
  {% if c.style_dna %}
  <div class=dnarule></div>
  <div class=dnak>Ключевые черты</div>
  <div class=traits>{% for d in c.style_dna[:4] %}<div><i>◆</i><span title="{{ d.note }}">{{ d.code }}</span></div>{% endfor %}</div>
  {% endif %}
 </div>

 <div class=panel>
  <h2 class=ph>Индекс настройки образа</h2>
  {% if idx is not none %}
  <div class=idxwrap>
   <svg viewBox="0 0 120 120" class=idxring aria-hidden=true>
    <circle cx="60" cy="60" r="52" fill=none stroke="#eee3cf" stroke-width="11"/>
    <circle cx="60" cy="60" r="52" fill=none stroke="#5D2230" stroke-width="11" stroke-linecap=round
            transform="rotate(-90 60 60)" stroke-dasharray="327"
            stroke-dashoffset="{{ (327 - 327 * idx / 100)|round|int }}"/>
    <text x=60 y=70 text-anchor=middle class=idxnum>{{ idx }}%</text>
   </svg>
   <div>
    {# Формулировка по величине индекса: не хвалим ради похвалы, а называем, где клиентка стоит. #}
    <p class=idxtext>{% if idx >= 80 %}Ты уже близко. Образ почти совпадает с тем, как ты хочешь считываться.{% elif idx >= 60 %}Хороший результат, гармония близка. Стилевая основа уже собрана — осталось убрать то, что размывает впечатление.{% elif idx >= 40 %}Половина образа уже работает на тебя. Остальное — зона настройки, которую мы закрываем ниже.{% else %}Зона настройки большая, и это твой ресурс: изменения будут заметны сразу.{% endif %}</p>
    {# Разрыв называем числом, а не только кольцом: Карта обязана показывать тот же Gap,
       что посчитал квиз, иначе диагностика и результат расходятся. #}
    <p class=idxtext>Оставшиеся {{ c.gap }}% настраиваются через выбранную<br>капсулу и первые покупки под твою формулу.</p>
    <a class=idxmore href="#howto">Как повысить индекс →</a>
   </div>
  </div>
  {% else %}
  <p class=psub style="margin-top:12px">Индекс появится после замера в квизе.</p>
  {% endif %}
 </div>

 <div class=panel>
  <h2 class=ph>Какой эффект ты хочешь производить</h2>
  <p class=idxtext style="margin-top:12px">Ты выбрала воздействие, которое работает<br>на твои цели и характер.</p>
  {% if c.want_traits %}
  <div class=effchips>{% for t in c.want_traits %}<span class=effchip>{{ t }}</span>{% endfor %}</div>
  {% elif c.emphasize %}
  <p class=psub style="margin-top:10px">{{ c.emphasize }}</p>
  {% endif %}
 </div>

</div>

{# ── две рабочие колонки ─────────────────────────────────────────────────────────────── #}
<div class=cols>

 {# ── левая: что носить ───────────────────────────────────────────────────────────── #}
 <div class=col>

  <div>
   <h2 class=secttl id=looks>Образы под роли жизни</h2>
   {# Ни одного отрисованного образа — зовём догенерить: без этого клиентка остаётся с текстом
      и не знает, что образы на ней ещё можно получить. #}
   {% if not shared and c.looks and not c.looks|selectattr('img')|list %}
   <div class=noimg><b>Здесь пока только текст — без образов на тебе.</b>
    <div>Загрузи фото — соберём эти же образы на тебе.</div>
    <a href="/card?rebuild=1">Добавить образы →</a></div>
   {% endif %}
   {% if c.looks %}
   <div class=looksgrid>
    {% for lk in c.looks[:6] %}
    <a class=lookcard href="#look{{ loop.index }}">
     {% set li = lk.img or lk.get('preview_img') %}
     {% if li %}<img class=lookpic src="{{ li }}" alt="Образ · {{ lk.scenario or lk.title }}" loading=lazy>
     {% else %}<span class="lookpic empty"></span>{% endif %}
     <div class=lookbody>
      <div class=lookttl>{% set ltl = lk.scenario or lk.title or lk.name or '' %}<span class=lt>{{ ltl[0]|upper }}{{ ltl[1:] }}</span><span class=chev>›</span></div>
      <p class=lookdesc>{{ lk.why_it_works or lk.description or (lk['items']|join(' · ') if lk.get('items') else '') }}</p>
      {# Процент совпадения убран: считать его честно не на чем. Метрика сверяла палитру и
         силуэт ПО ТЕКСТУ названия вещи, а вещи каталога называются как в фиде («Приталенный
         двубортный жакет из лёгкой ткани») — ни цвета палитры, ни термина силуэта там нет.
         Все оси давали ноль, и система ставила собственному образу 8%. Показываем то, что
         правда: чего в образе не хватает до полного комплекта. #}
      {% if lk.missing_items %}<div class=lookmatch>добавить: {{ lk.missing_items|join(', ') }}</div>{% endif %}
     </div>
    </a>
    {% endfor %}
   </div>
   {% else %}
   <div class=panel><p class=psub>Образы появятся вместе со сборкой Карты.</p></div>
   {% endif %}
  </div>

  <div class=buyrow>
   <div class=panel>
    <h2 class=ph>Первые покупки под твою Формулу<a class=more href="#shopping">Смотреть все рекомендации →</a></h2>
    <p class=psub>Точечные вещи, которые усиливают капсулу и закрывают пробелы.</p>
    {% if c.shopping %}
    <div class=buygrid>
     {% for it in c.shopping[:3] %}
     <div class=buycard>
      {% set bimg = it.item_name|item_img %}
      {% if bimg %}<img class=buyimg src="{{ bimg }}" alt="{{ it.item_name }}" loading=lazy>
      <div class=capexample style="margin:-4px 0 6px">пример типа вещи</div>{% endif %}
      <div class=buyname>{{ it.item_name[0]|upper }}{{ it.item_name[1:] }}</div>
      {% if it.closes_gap %}<p class=buywhy><b>Почему подходит:</b> {{ it.closes_gap }}</p>{% endif %}
      {% if it.links %}<div class=buylinks><a href="{{ it.links.wildberries }}" target=_blank rel=noopener>WB</a> · <a href="{{ it.links.lamoda }}" target=_blank rel=noopener>Lamoda</a> · <a href="{{ it.links.ozon }}" target=_blank rel=noopener>Ozon</a></div>{% endif %}
     </div>
     {% endfor %}
    </div>
    {% else %}<p class=psub style="margin-top:12px">Список покупок появится вместе со сборкой Карты.</p>{% endif %}
   </div>

   <div class=panel id=palette>
    <h2 class=ph>Твоя палитра</h2>
    <p class=psub>Базовые, акцентные и нейтральные цвета.</p>
    {% for grp, title in [('base','Базовые'),('main','Основные'),('accent','Акцентные')] %}
     {% set items = c.palette|selectattr('group','equalto',grp)|list %}
     {% if items %}<div class=palgrp>{{ title }}</div>
     <div class=palrow>{% for p in items %}<span class=c style="background:{{ p.hex }}" title="{{ p.name }}"></span>{% endfor %}</div>{% endif %}
    {% endfor %}
    {% set rest = c.palette|rejectattr('group','in',['base','main','accent'])|list %}
    {% if rest %}<div class=palgrp>Ещё в палитре</div>
    <div class=palrow>{% for p in rest %}<span class=c style="background:{{ p.hex }}" title="{{ p.name }}"></span>{% endfor %}</div>{% endif %}
    <a class=idxmore href="#howto">Как использовать палитру →</a>
   </div>
  </div>

 </div>

 {# ── правая: на чём это держится ─────────────────────────────────────────────────── #}
 <div class=col>

  <div class=panel id=capsule>
   <h2 class=ph>Опорная капсула {{ c.starter_capsule_count or (c.starter_capsule|length if c.starter_capsule else 0) }} вещей<span class=dot>◈</span></h2>
   <p class=psub>Стилевая основа, которая собирает твои образы.</p>
   {# Плашка обещала «эта капсула собрана из образов выше» — но капсула подбирается из каталога
      под Формулу и палитру. На самом деле капсула ЧЕСТНО собирается из вещей образов
      (_core_capsule_from_looks) — терялись только сценарии, поэтому связь была не видна.
      Теперь под каждой вещью написано, в каких образах она работает. #}
   <div class=caporigin>
    <i>↺</i>
    <div><b>Собрана из твоих образов, а не отдельно от них.</b><span>Мы разложили образы выше на вещи и оставили те, что работают сразу в нескольких сценариях — это и есть опора гардероба. Под каждой вещью видно, где она работает.</span></div>
   </div>
   {% if c.starter_capsule %}
   <div class=capgrid>
    {% for it in c.starter_capsule[:6] %}
    <div class=capcard>
     {# Кадр из нашей библиотеки иллюстрирует ТИП вещи, а не её цвет: жакет графитового
        цвета показан бежевым. Молчать об этом нечестно — помечаем. #}
     {% set fromlib = (not it.image) and (it.name|item_img) %}
     {% set isexample = it.image_is_example or fromlib %}
     {% set lib = it.image or fromlib %}
     {% if lib %}<img class=capimg src="{{ lib }}" alt="{{ it.name }}" loading=lazy>
     {% else %}<span class="capimg empty">{{ it.slot or 'вещь' }}<br>без фото</span>{% endif %}
     {# Бейдж говорит, ЗАЧЕМ вещь в капсуле: опора работает в нескольких образах, остальное — дополняет образы. #}
     {% if it.capsule_role == 'core' %}<span class=capbadge>опора</span>
     {% elif it.outfits_count and it.outfits_count > 1 %}<span class=capbadge>собирает {{ it.outfits_count }} образ{{ 'а' if it.outfits_count % 10 in [2,3,4] and it.outfits_count // 10 != 1 else 'ов' }}</span>
     {% elif loop.first %}<span class=capbadge>купить первой</span>{% endif %}
     <div class=capbody>
      <div class=capname>{{ it.name }}</div>
      {% if it.scenarios %}<div class=capscen>{% for sc in it.scenarios[:3] %}<span>{{ sc }}</span>{% endfor %}</div>{% endif %}
      {% if isexample %}<div class=capexample>пример типа вещи</div>{% endif %}
      {# Названия брендов не показываем: партнёрство с ними не согласовано, а Карта продаёт метод,
         а не витрину конкретного магазина. Вещь, фото и цена остаются — по ним видно, что подобрано
         реальное, а не абстрактное. Найти вещь помогает поиск по маркетплейсам ниже. #}
      {% if it.price %}<div class=capprice>{{ '{:,}'.format(it.price).replace(',',' ') }} ₽</div>{% endif %}
      {% if it.search %}<div class=capfind><a href="{{ it.search.wildberries }}" target=_blank rel=noopener>WB</a> · <a href="{{ it.search.lamoda }}" target=_blank rel=noopener>Lamoda</a></div>{% endif %}
     </div>
    </div>
    {% endfor %}
   </div>
   {% else %}<p class=psub style="margin-top:12px">Опорная капсула соберётся вместе с образами.</p>{% endif %}
  </div>

  {% if c.capsule_combos %}
  <div class=panel>
   <h2 class=ph>Из {{ c.starter_capsule_count }} вещей — {{ c.combination_count }} {{ 'сочетание' if c.combination_count % 10 == 1 and c.combination_count % 100 != 11 else 'сочетания' if c.combination_count % 10 in [2,3,4] and c.combination_count % 100 not in [12,13,14] else 'сочетаний' }}<a class=more href="#combos">Показать все →</a></h2>
   <p class=psub>Примеры ready-to-wear сочетаний из твоей опорной капсулы.</p>
   {# Экономика капсулы: главный ответ на «дорого». Числа считаются кодом и проверяются на
      бумаге — вся капсула ÷ число образов, и сколько вещей не понадобилось. #}
   {% if econ %}
   <div class=econ>
    {% if econ.has_prices %}<div class=econcell><b>{{ '{:,}'.format(econ.cost_per_look).replace(',', ' ') }} ₽</b><span>стоит один собранный образ</span></div>{% endif %}
    <div class=econcell><b>{{ econ.saved_items }}</b><span>вещей не пришлось покупать: на {{ econ.looks }} отдельных {{ 'комплект' if econ.looks % 10 == 1 and econ.looks % 100 != 11 else 'комплекта' if econ.looks % 10 in [2,3,4] and econ.looks % 100 not in [12,13,14] else 'комплектов' }} ушло бы {{ econ.standalone_items }}</span></div>
   </div>
   {% endif %}
   {# Матрица «база × слой»: капсула списком читается как шопинг-лист, а здесь видно, ради чего
      она собрана — одни и те же вещи дают разные образы под разные роли. Считается кодом. #}
   {% if matrix %}
   <div class=matrix>
    <div class=matrixhead>Из {{ matrix.items_count }} вещей — {{ matrix.total }} {{ 'образ' if matrix.total % 10 == 1 and matrix.total % 100 != 11 else 'образа' if matrix.total % 10 in [2,3,4] and matrix.total % 100 not in [12,13,14] else 'образов' }}<span>одна база — разные роли</span></div>
    {% for row in matrix.rows[:4] %}
    <div class=matrixrow>
     <div class=matrixbase>{{ row.base }}</div>
     <div class=matrixcells>
      {% for cell in row.cells %}
      <div class=matrixcell>
       <span class=mcrole>{{ cell.role }}</span>
       <span class=mcwhy>{{ cell.why }}</span>
      </div>
      {% endfor %}
     </div>
    </div>
    {% endfor %}
   </div>
   {% endif %}
   <div class=combolane>
    {% for combo in c.capsule_combos[:6] %}
    <div class=combo title="{{ combo.title }}">
     <div class=combopics>
      {% for it in combo['items'][:3] %}
       {% set ci = it.image or (it.name|item_img) %}{% if ci %}<img src="{{ ci }}" alt="{{ it.name }}" loading=lazy>{% else %}<span class=combodot title="{{ it.name }}"></span>{% endif %}
      {% endfor %}
     </div>
     <div class=combotitle>{{ combo.title }}</div>
     <div class=combodesc>{{ combo.summary or (combo['items']|map(attribute='name')|join(' · ')) }}</div>
    </div>
    {% endfor %}
   </div>
  </div>
  {% endif %}

  {# «Что уводит» — стоп-цвета и стоп-лист в одном месте: клиентке важнее увидеть запрет
     рядом с капсулой, чем отдельной главой в конце. #}
  {% set stopn = (c.stop_colors|length if c.stop_colors else 0) + (c.stop_list|length if c.stop_list else 0) %}
  {% if stopn %}
  <div class=panel>
   <h2 class=ph>Что уводит от твоей Формулы</h2>
   <p class=psub>Избегай этих элементов — они нарушают твою гармонию.</p>
   <div class=stopgrid>
    {% for p in (c.stop_colors or [])[:5] %}
    <div class=stopcell>
     <div class=stopbox><span class=sw style="background:{{ p.hex }}"></span></div>
     <span class=stopx>✕</span>
     <div class=stopcap title="{{ p.why }}">{{ p.name }}</div>
    </div>
    {% endfor %}
    {# Стоп-цвет показываем плашкой цвета, а запрет из стоп-листа — текстом внутри плитки:
       картинок «чего не носить» у нас нет, а пустая бежевая рамка читается как поломка. #}
    {% for s in (c.stop_list or [])[:5 - (c.stop_colors|length if c.stop_colors else 0)] %}
    <div class=stopcell>
     <div class="stopbox text"><span title="{{ s }}">{{ s }}</span></div>
     <span class=stopx>✕</span>
    </div>
    {% endfor %}
   </div>
   <a class=idxmore href="#stoplist">Смотреть стоп-лист целиком →</a>
  </div>
  {% endif %}

 </div>
</div>

{# ── нижняя лента ────────────────────────────────────────────────────────────────────── #}
{# Постоянная ссылка: без неё Карта достижима только из того браузера, где её собрали.
   На чужом экране (открыли по ссылке) блок скрыт — там нечего копировать и некуда вести. #}
{% if not shared and card_link %}
<div class=sharebox>
 <div>
  <div class=sharek>Ссылка на твою Карту</div>
  <p class=sharep>Открывается на любом устройстве и остаётся рабочей, даже если ты пересоберёшь Карту. Сохрани её.</p>
 </div>
 <div class=sharerow>
  <input id=sharelink class=shareinput readonly value="{{ card_link }}" aria-label="Ссылка на Карту">
  <button type=button class=sharebtn onclick="copyLink(this)">Скопировать</button>
 </div>
</div>
{% endif %}

<div class=footband>
 <div class=mono>SS</div>
 <div class=foottext>Твоя Карта стиля — это персональная стратегия.<br>Она работает на тебя каждый день.</div>
 <div class=footbtns>
  <button class=btnfill id=pdfbtn onclick="downloadPdf()">Скачать PDF <span>⬇</span></button>
  {% if not shared %}<a class=btnline href="/cabinet">Перейти в Стиль каждый день <span>→</span></a>
  {% else %}<a class=btnline href="/quiz">Собрать свою Карту <span>→</span></a>{% endif %}
 </div>
</div>

{# ── разбор: глубина Карты, свёрнутая по разделам ───────────────────────────────────── #}

{% if c.looks %}
{# Закрыт по умолчанию: Карта — панель на один экран, а полный разбор образов раскрывается
   по клику (и принудительно раскрывается перед печатью в PDF). #}
<details class=deep id=combos>
 <summary>Образы целиком <span>состав, сценарии и что добавить в капсулу</span></summary>
 <div class=deepbody>
  {% for lk in c.looks %}
  <div class=panel style="margin-bottom:12px" id="look{{ loop.index }}">
   <h3 style="margin-top:0">{{ lk.scenario or lk.title or lk.name }}</h3>
   {# Раскладка образа: слева он на клиентке, справа — вещи, из которых собран (flat-lay).
      Это связывает образ с капсулой: клиентка видит, что образ собран из её же вещей. #}
   <div class="lookflat{% if not lk.img %} lookflat-noimg{% endif %}">
    {% if lk.img %}<div class=lookmodel><img src="{{ lk.img }}" alt="Образ на тебе"></div>{% endif %}
    {% if lk.pieces %}
    <div class=lookpieces>
     {% for pc in lk.pieces %}
     <div class=lookpiece>
      {% if pc.image %}<img src="{{ pc.image }}" alt="{{ pc.name }}" loading=lazy>
      {% else %}<span class="lookpiece-ph"><i>{{ pc.slot }}</i><b>{{ pc.name }}</b></span>{% endif %}
      <span class=lookpiece-nm>{{ pc.name }}</span>
     </div>
     {% endfor %}
    </div>
    {% endif %}
   </div>
   {% if lk.why_it_works or lk.description %}<p style="margin-top:12px">{{ lk.why_it_works or lk.description }}</p>{% endif %}
   {% if lk.missing_items %}<p style="color:var(--muted)">Если добавить в капсулу: {{ lk.missing_items|join(' · ') }}</p>{% endif %}
  </div>
  {% endfor %}
  {% if c.styling and c.styling.looks %}
  <h3>Одна вещь — два образа</h3>
  <p>{% if c.styling.idea %}{{ c.styling.idea }}{% else %}Одна опорная вещь{% if c.styling.base_item %} ({{ c.styling.base_item }}){% endif %} — два разных образа.{% endif %} Так работает капсула: меньше вещей, больше готовых решений.</p>
  {% for lk in c.styling.looks %}
  <div class=panel style="margin-bottom:12px">
   <h3 style="margin-top:0">{{ lk.title or lk.name or lk.scenario }}</h3>
   {# Образ на клиентке + раскладка его вещей одной картинкой. Раскладка генерируется вместе
      с образом, поэтому вещи на ней ИМЕННО ТЕ, что в составе, — в отличие от коллажа
      каталожных фото с разными фонами и рекламным текстом поверх вещи. #}
   <div class=pairrow>
    {% if lk.img %}<img class=pairmodel src="{{ lk.img }}" alt="Образ на тебе">{% endif %}
    {% if lk.flatlay %}<img class=pairflat src="{{ lk.flatlay }}" alt="Вещи этого образа">{% endif %}
   </div>
   {% if lk.items %}<p class=pairitems>{{ lk.items|join(' · ') }}</p>{% endif %}
   {% if lk.description %}<p>{{ lk.description }}</p>{% endif %}
  </div>
  {% endfor %}
  {% endif %}
 </div>
</details>
{% endif %}

<details class=deep id=howto>
 <summary>Как повысить индекс <span>кто ты, твоя фигура, работа с палитрой</span></summary>
 <div class=deepbody>
  {% if c.substyle_rationale %}<h3>Почему это твой стиль</h3><p>{{ c.substyle_rationale }}</p>{% endif %}
  {% if c.personality and c.personality.portrait %}<p>{{ c.personality.portrait }}</p>
   {% if c.personality.style_implications %}<ul class=clean>{% for s in c.personality.style_implications %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}{% endif %}
  {% if c.figure or c.figure_fit or c.silhouettes %}
  <h3 id=figure>Что носить по твоей фигуре</h3>
  {% if c.figure %}<p>Геометрия: <b>{{ c.figure }}</b> — по этим правилам подобраны образы и капсула, чтобы вещи сидели по твоим пропорциям.</p>{% endif %}
  <ul class=clean>
   {% if c.emphasize %}<li><b>Твой акцент:</b> {{ c.emphasize }} — образы строим вокруг этого</li>{% endif %}
   {% if c.figure_fit %}<li><b>Подчёркиваем:</b> {{ c.figure_fit.emphasize }}</li>
   <li><b>Баланс:</b> {{ c.figure_fit.balance }}</li>
   <li><b>Посадка и размеры:</b> {{ c.figure_fit.fit }}</li>{% endif %}
  </ul>
  {% set sils = c.figure_fit.silhouettes if (c.figure_fit and c.figure_fit.silhouettes) else c.silhouettes %}
  {% if sils %}<p style="color:var(--muted)">Твои силуэты:</p><ul class=clean>{% for s in sils %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}
  {% endif %}
  <h3>Как работает палитра</h3>
  {% if c.colortype %}<p>Твой цветотип — <b>{{ c.colortype }}</b>.{% if c.contrast %} Контраст {{ c.contrast }}.{% endif %} На нём построена палитра.</p>{% endif %}
  <p>Основа гардероба — это спокойные вещи, на которых строится весь total look. Основные оттенки мягко сочетаются с основой, акценты работают точечно: один на образ, не больше.</p>
  {% for grp, title in [('base','База и нейтрали'),('main','Основные'),('accent','Акценты')] %}
   {% set items = c.palette|selectattr('group','equalto',grp)|list %}
   {% if items %}<div class=palgrp>{{ title }}</div><div class=swatches>
    {% for p in items %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}
   </div>{% endif %}
  {% endfor %}
  {% if c.style_reference %}
  <h3>Стилевой ориентир</h3>
  <div class=ref>
   <div class=refname>{{ c.style_reference.name }}</div>
   {% if c.style_reference.match_axis_1_impression %}<p class=refline>По впечатлению: {{ c.style_reference.match_axis_1_impression }}</p>{% endif %}
   {% if c.style_reference.match_axis_2_physical %}<p class=refline>По параметрам: {{ c.style_reference.match_axis_2_physical }}</p>{% endif %}
  </div>{% endif %}
 </div>
</details>

{% if c.shopping %}
<details class=deep id=shopping>
 <summary>Топ покупок под твою Формулу <span>{{ c.shopping|length }} приоритетов и ориентир по бюджету</span></summary>
 <div class=deepbody>
  <div class=shopfull>
   {% for it in c.shopping %}
   <div class=si>
    <div class=sn>{{ it.item_name }}</div>
    {% if it.closes_gap %}<div class=sy>{{ it.closes_gap }}</div>{% endif %}
    {% if it.links %}<div class=sl>Найти: <a href="{{ it.links.wildberries }}" target=_blank rel=noopener>WB</a> · <a href="{{ it.links.lamoda }}" target=_blank rel=noopener>Lamoda</a> · <a href="{{ it.links.ozon }}" target=_blank rel=noopener>Ozon</a></div>{% endif %}
   </div>
   {% endfor %}
  </div>
  {% if c.budget and c.budget.min %}<p style="margin-top:12px;color:var(--muted)">Ориентир по бюджету: {{ '{:,}'.format(c.budget.min).replace(',',' ') }}–{{ '{:,}'.format(c.budget.max).replace(',',' ') }} ₽{% if c.budget.note %} · {{ c.budget.note }}{% endif %}</p>{% endif %}
 </div>
</details>
{% endif %}

{% if c.stop_list or c.stop_colors %}
<details class=deep id=stoplist>
 <summary>Стоп-лист целиком <span>что гасит тебя и почему</span></summary>
 <div class=deepbody>
  {% if c.stop_colors %}
  <h3>Стоп-цвета</h3>
  <div class=swatches>
   {% for p in c.stop_colors %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm><b>{{ p.name }}</b><br>{{ p.why }}</div></div>{% endfor %}
  </div>{% endif %}
  {% if c.stop_list %}<h3>Что не носить</h3><ul class="clean stop">{% for s in c.stop_list %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}
 </div>
</details>
{% endif %}

{% if not shared %}<div class=fbblock id=fbblock>
{% if thanks %}
 <p style="margin:0;font-size:15px">Спасибо. Твой отзыв записан — он помогает делать Карту точнее.</p>
{% else %}
 <h2>Как тебе Карта?</h2>
 <p class=h>Оцени и напиши пару слов — что откликнулось, чего не хватило.</p>
 <form method=post action="/card/feedback">
  <div style="display:flex;gap:10px;margin-bottom:12px">
   {% for n in [1,2,3,4,5] %}<label style="display:inline-flex;align-items:center;gap:5px;font-size:14px"><input type=radio name=rating value="{{ n }}" style="width:auto">{{ n }}</label>{% endfor %}
  </div>
  <textarea name=text rows=3 placeholder="Что откликнулось, чего не хватило?"></textarea>
  <button type=submit>Отправить отзыв</button>
 </form>
{% endif %}
</div>
{% endif %}

</div></div>
<script>
function copyLink(btn){
  var f=document.getElementById('sharelink'); if(!f) return;
  f.select(); f.setSelectionRange(0, 99999);
  var done=function(){ var t=btn.textContent; btn.textContent='Скопировано'; btn.classList.add('done');
    setTimeout(function(){ btn.textContent=t; btn.classList.remove('done'); }, 1800); };
  // navigator.clipboard живёт только на https и localhost — на http падает, поэтому фолбэк
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(f.value).then(done, function(){ try{document.execCommand('copy'); done();}catch(e){} });
  } else { try{ document.execCommand('copy'); done(); }catch(e){} }
}
function downloadPdf(){
  // Печать браузера вместо html2canvas: даёт НАСТОЯЩИЙ PDF с векторным текстом на любом
  // устройстве. @media print разворачивает дашборд в один поток и раскрывает разделы «разбор»,
  // поэтому в файл попадает вся Карта, а не только видимая панель.
  document.querySelectorAll('details.deep').forEach(function(d){ d.open = true; });
  var imgs=Array.prototype.slice.call(document.querySelectorAll('img'));
  var waits=imgs.map(function(img){
    if(img.complete && img.naturalWidth>0) return img.decode?img.decode().catch(function(){}):Promise.resolve();
    return new Promise(function(res){ img.onload=function(){ (img.decode?img.decode().catch(function(){}):Promise.resolve()).then(res); }; img.onerror=res; });
  });
  var go=function(){ window.print(); };
  (document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve()).then(function(){
    Promise.all(waits).then(go, go);
  }, go);
}
// Ссылки «Как повысить индекс», «Показать все» и т.п. ведут в свёрнутый раздел — раскрываем его,
// иначе клик по якорю прокручивает к закрытому заголовку и выглядит как сломанная ссылка.
document.addEventListener('click', function(e){
  var a = e.target.closest && e.target.closest('a[href^="#"]');
  if(!a) return;
  var t = document.querySelector(a.getAttribute('href'));
  if(!t) return;
  var d = t.closest('details');
  if(d) d.open = true;
  var dp = t.tagName === 'DETAILS' ? t : null;
  if(dp) dp.open = true;
});
</script>
</body></html>"""


# Экран «нужна диагностика». Раньше /card и /cabinet молча редиректили на квиз с ?fresh=1 —
# человек жал «Карта стиля» и оказывался в начале квиза без единого слова о том, почему.
# Фаундер: «нажимаю на кнопки и переходит на квиз... нужно грамотно проработать путь клиента».
NEED_DIAGNOSIS = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>{{ title }} — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.6}
 .wrap{max-width:640px;margin:0 auto;padding:40px 26px 80px}
 .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:40px}
 .logo{font-family:'Cormorant Garamond',serif;font-size:22px}
 .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .card{background:#fff;border:1px solid var(--line);border-radius:22px;padding:34px 32px;box-shadow:0 12px 30px rgba(31,22,20,.04)}
 .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--wine)}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:34px;line-height:1.1;margin:10px 0 14px}
 p{margin:0 0 14px;color:#4e473f}
 .steps{margin:22px 0 26px;padding:0;list-style:none}
 .steps li{display:flex;gap:12px;align-items:flex-start;margin:12px 0;font-size:15px}
 .num{flex:0 0 26px;height:26px;border-radius:50%;background:var(--cream);color:var(--wine);
      display:flex;align-items:center;justify-content:center;font-size:13px;border:1px solid var(--line)}
 .done .num{background:var(--wine);color:#fff;border-color:var(--wine)}
 .btn{display:inline-block;padding:15px 30px;background:var(--wine);color:#fff;border-radius:10px;
      text-decoration:none;font-size:16px}
 .meta{font-size:13px;color:var(--muted);margin-top:16px}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>
<div class=card>
 <div class=eyebrow>{{ eyebrow }}</div>
 <h1>{{ title }}</h1>
 <p>{{ lead }}</p>
 <ul class=steps>
  <li class=done><span class=num>1</span><span>Диагностика — 14 вопросов и фото. Отсюда берётся твоя Формула стиля, цветотип и силуэт.</span></li>
  <li><span class=num>2</span><span>Карта стиля — палитра, капсула и образы на тебе. Строится на результатах диагностики.</span></li>
  <li><span class=num>3</span><span>Стиль каждый день — живой гардероб, где формула работает в обычной жизни.</span></li>
 </ul>
 <a class=btn href="/quiz">Пройти диагностику</a>
 <p class=meta>5 минут, без регистрации. Результат сохранится за тобой.</p>
</div>
</div></body></html>"""

CARD_BUILD_FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Собрать Карту стиля</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 /* Один язык с новой Картой: те же токены, спокойный ритм, секции вместо длинного списка. */
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--wine2:#7A2438;--muted:#6b645c;
       --line:#e6dfd2;--sand:#F3ECDF;--soft:#FBF6EC}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;margin:0;background:var(--cream);
      color:var(--ink);line-height:1.6;-webkit-font-smoothing:antialiased}
 .wrap{max-width:900px;margin:0 auto;padding:30px 22px 110px}
 /* Вопросы парами на десктопе: анкета в одну колонку на широком экране выглядит
    бесконечной лентой, хотя половина вопросов — короткие наборы чипов. */
 .qgrid{display:grid;grid-template-columns:1fr 1fr;gap:22px 28px;align-items:start}
 .qgrid .q{min-width:0;display:flex;flex-direction:column;gap:12px;background:var(--soft);
           border:1px solid var(--line);border-radius:18px;padding:18px 18px 16px}
 .qwide{grid-column:1/-1}
 @media(max-width:760px){.qgrid{grid-template-columns:1fr}.qgrid .q{padding:16px}}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-family:'Cormorant Garamond',serif;font-size:22px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .eyebrow{font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--wine);margin:24px 0 9px}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:40px;line-height:1.05;
    margin:8px 0 12px;letter-spacing:-.015em}
 .lead{color:var(--muted);margin:0 0 8px;font-size:15px;line-height:1.6}
 /* карточка-секция: у каждой части анкеты своя, между ними воздух */
 .card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:4px 24px 26px;margin-top:16px}
 .sect{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px 26px 26px;margin-top:16px}
 .secth{font-family:'Cormorant Garamond',serif;font-size:23px;line-height:1.2;margin:0 0 4px;
        display:flex;align-items:baseline;gap:11px}
 /* номер шага: длинная анкета должна показывать, сколько ещё осталось */
 .secth i{flex:0 0 auto;font-style:normal;font-family:Onest,sans-serif;font-size:11px;
          letter-spacing:.14em;color:var(--wine);border:1px solid var(--line);border-radius:999px;
          padding:4px 10px;background:var(--soft)}
 .sectd{font-size:13px;color:var(--muted);margin:0 0 6px;line-height:1.5}
 label{display:block;margin:20px 0 8px;font-size:14px;font-weight:400;color:#3f3931;line-height:1.5}
 .q > label{margin:0;min-height:4.4em;font-size:15px;color:#312c26;letter-spacing:-.01em}
 @media(max-width:760px){.q > label{min-height:auto}}
 .fld{width:100%;padding:12px 13px;border:1px solid #d9d2c7;border-radius:10px;font-family:inherit;font-size:15px;color:var(--ink);background:#fff;transition:border-color .15s}
 .fld:focus{outline:0;border-color:var(--wine)} .fld::placeholder{color:#a89f92}
 select.fld{appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b645c' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:34px}
 /* Четыре стиля в ряд: в две колонки кадры 3:4 занимали почти два экрана и анкета
    начиналась с бесконечной прокрутки. На телефоне остаются пары. */
 .stylegrid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:6px 0 6px}
 @media(max-width:760px){.stylegrid{grid-template-columns:1fr 1fr}}
 .stylecard{position:relative;margin:0;cursor:pointer;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff;transition:border-color .15s,box-shadow .15s}
 .stylecard input{position:absolute;opacity:0;pointer-events:none}
 .stylepic{display:block;aspect-ratio:3/4;background-size:cover;background-position:top center;background-color:#eee6d8}
 .stylemeta{display:block;padding:9px 11px;font-size:13.5px;line-height:1.35}
 .stylehint{color:var(--muted);font-size:11.5px;line-height:1.3}
 .stylecard:has(input:checked){border-color:var(--wine);box-shadow:0 0 0 2px var(--wine)}
 .stylecard:has(input:checked)::after{content:'✓';position:absolute;top:8px;right:8px;width:24px;height:24px;border-radius:50%;background:var(--wine);color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px}
 .chips{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin:0}
 .chips.cols3{grid-template-columns:repeat(3,minmax(0,1fr))}
 .chips.cols4{grid-template-columns:repeat(4,minmax(0,1fr))}
 @media(max-width:760px){.chips,.chips.cols3,.chips.cols4{grid-template-columns:repeat(2,minmax(0,1fr))}}
 @media(max-width:520px){.chips,.chips.cols3,.chips.cols4{grid-template-columns:1fr}}
 .chip{position:relative;cursor:pointer;margin:0}
 .chip input{position:absolute;opacity:0;pointer-events:none;width:0;height:0}
 .chip span{display:flex;align-items:center;justify-content:center;width:100%;min-height:54px;
            padding:10px 16px;border:1px solid #dfd5c6;border-radius:18px;
            font-size:13.5px;color:#4e473f;background:#fff;user-select:none;text-align:center;
            box-shadow:0 1px 0 rgba(93,34,48,.03);
            transition:background .15s,color .15s,border-color .15s,box-shadow .15s,transform .15s}
 .chip span:hover{border-color:#c9bda9;box-shadow:0 6px 16px rgba(93,34,48,.06);transform:translateY(-1px)}
 .chip input:checked+span{background:var(--wine);color:#fff;border-color:var(--wine)}
 .chip input:focus-visible+span{box-shadow:0 0 0 2px rgba(93,34,48,.35)}
 .substep{margin:30px 0 10px;padding-top:6px;border-top:1px solid rgba(93,34,48,.08)}
 .substep:first-of-type{margin-top:26px}
 .subcopy{font-size:13px;color:var(--muted);margin:0 0 14px;line-height:1.55}
 .traitlist{display:grid;grid-template-columns:1fr;gap:12px}
 .traitcard{background:var(--soft);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
 .traitq{font-size:14px;color:#312c26;line-height:1.45;margin:0}
 .traitscale{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:12px}
 .scalechip{position:relative;cursor:pointer}
 .scalechip input{position:absolute;opacity:0;pointer-events:none;width:0;height:0}
 .scalechip span{display:flex;align-items:center;justify-content:center;min-height:42px;border:1px solid #dfd5c6;
                 border-radius:14px;background:#fff;color:var(--muted);font-size:13px;transition:background .15s,color .15s,border-color .15s,box-shadow .15s}
 .scalechip input:checked+span{background:var(--wine);border-color:var(--wine);color:#fff}
 .scalechip input:focus-visible+span{box-shadow:0 0 0 2px rgba(93,34,48,.35)}
 .lifeg{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
 .lifef{margin:0;font-size:13px;font-weight:400;color:var(--muted)}
 .lifev{display:block;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--wine);margin-bottom:6px}
 @media(max-width:760px){.lifeg{grid-template-columns:1fr}.traitscale{grid-template-columns:repeat(5,minmax(0,1fr))}}
 @media(max-width:520px){.traitscale{grid-template-columns:repeat(3,minmax(0,1fr))}}
 /* Загрузка фото: своя зона вместо серой кнопки браузера — это первый экран продукта. */
 .file{position:relative;border:1.5px dashed #cdbfa6;border-radius:14px;padding:22px 18px;
       text-align:center;background:var(--soft);transition:border-color .15s,background .15s}
 .file:hover{border-color:var(--wine);background:#fff}
 .file input[type=file]{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}
 .fileico{font-size:22px;color:var(--wine);line-height:1}
 .filet{font-size:14.5px;color:var(--ink);margin-top:8px}
 .filet b{color:var(--wine);font-weight:500}
 .files{font-size:12.5px;color:var(--muted);margin-top:4px}
 /* Кнопка не должна теряться в конце длинной анкеты — держим её на виду. */
 .submitbar{position:sticky;bottom:0;margin-top:18px;padding:14px 0 10px;
            background:linear-gradient(180deg,rgba(245,239,227,0),var(--cream) 28%)}
 button{width:100%;padding:16px;background:var(--wine);color:#fff;border:0;border-radius:12px;
        font-family:inherit;font-size:16px;cursor:pointer;letter-spacing:.01em;
        box-shadow:0 8px 24px rgba(93,34,48,.18);transition:opacity .15s}
 button:hover{opacity:.92}
 .consent{font-size:13px;color:var(--muted);display:flex;gap:8px;margin-top:14px;line-height:1.4} .consent input{width:auto;margin-top:3px}
 .hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px} .hint a{color:var(--wine)}
 .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:8px}
 .notice{color:#5a4a2a;background:#f6efdf;border:1px solid #e3d3a8;padding:14px 16px;border-radius:10px;margin-bottom:8px;font-size:14.5px;line-height:1.5} .notice b{color:var(--wine)}
</style></head><body><div class=wrap>
{% macro chips(name, opts, cls='') %}<div class="chips {{ cls }}">{% for o in opts %}<label class=chip><input type=checkbox name="{{ name }}" value="{{ o }}"><span>{{ o }}</span></label>{% endfor %}</div>{% endmacro %}
<div class=top><span class=logo>Чувство стиля</span><a href="/me">← мой профиль</a></div>
<div class=eyebrow>Шаг 2 из 3 · Карта стиля</div>
<h1>Покажем тебя в 6 образах</h1>
<p class=lead>Это первый платный слой после диагностики. Загрузи фото в полный рост — соберём твою Карту стиля и покажем тебя в 6 образах под твои сценарии, чтобы у тебя сразу появился готовый результат, а не очередная теория.</p>
{% if notice %}<p class=notice>{{ notice|safe }}</p>{% endif %}
{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/card/build" enctype="multipart/form-data">
<div class=card>
 <div class=secth style="margin-bottom:10px"><i>ШАГ 1</i>Твоё фото</div>
 <label>Фото в полный рост</label>
 <div class=file>
  <input type=file name=photo accept="image/*" required onchange="var n=this.files[0]&&this.files[0].name;if(n){this.parentNode.querySelector('.filet').innerHTML='<b>'+n+'</b>';}">
  <div class=fileico>❐</div>
  <div class=filet>Перетащи фото сюда или <b>выбери файл</b></div>
  <div class=files>JPG или PNG, в полный рост</div>
 </div>
 <p class=hint style="text-align:left;margin:10px 0 0">Лицо должно быть хорошо видно — крупно, при дневном свете, без тёмных очков и сильной тени. От этого зависит сходство в образах.</p>
</div>

<div class=sect>
 <div class=secth><i>ШАГ 2</i>Чтобы Карта была точнее</div>
 <p class=sectd>Всё ниже — по желанию. Но чем больше расскажешь, тем точнее соберутся образы.</p>
 <label>Какие образы тебе откликаются? Отметь, к чему тяготеешь — образы соберём в этом характере.</label>
 <div class=stylegrid>
  {% for s in style_cards %}
  <label class=stylecard>
   <input type=checkbox name=want_styles value="{{ s.code }}">
   <span class=stylepic style="background-image:url('/photos/styles/{{ s.code }}.png')"></span>
   <span class=stylemeta><b>{{ s.label }}</b><br><span class=stylehint>{{ s.hint }}</span></span>
  </label>
  {% endfor %}
 </div>
 <div class=qgrid>
  <div class=q>
   <label>{% if current_colortype_label %}Твой цветотип по фото — <b>{{ current_colortype_label }}</b>. Если знаешь свой сезон и он другой, выбери его — палитра пересоберётся:{% else %}Знаешь свой цветотип? Выбери сезон, и палитра соберётся под него (по желанию):{% endif %}</label>
   <select name=colortype_override class=fld>
    <option value="">{% if current_colortype_label %}— оставить как есть —{% else %}— определим по фото —{% endif %}</option>
    {% for code, lab in colortype_options %}<option value="{{ code }}">{{ lab }}</option>{% endfor %}
   </select>
  </div>
  <div class=q>
   <label>На какой сезон собрать капсулу?</label>
   <div class="chips cols4">
  <label class=chip><input type=radio name=season value=spring><span>Весна</span></label>
  <label class=chip><input type=radio name=season value=summer><span>Лето</span></label>
  <label class=chip><input type=radio name=season value=autumn checked><span>Осень</span></label>
  <label class=chip><input type=radio name=season value=winter><span>Зима</span></label>
   </div>
  </div>
 </div>
 <div class=qgrid>
  <div class=q>
   <label>Что в твоей внешности подчеркнуть? Отметь, что нравится.</label>
   {{ chips('adv', ['талию','ноги','плечи','шею и декольте','запястья','осанку','грудь','бёдра']) }}
  </div>
  <div class=q>
   <label>Что визуально уравновесить?</label>
   {{ chips('balance', ['плечи и бёдра','талию','добавить рост','смягчить плечи','объём сверху','объём снизу']) }}
  </div>
  <div class=q>
   <label>Что ты точно не носишь? Уберём из образов.</label>
   {{ chips('taboo', ['мини','глубокое декольте','каблук выше 5 см','обтягивающее','яркие принты','красный','прозрачное','оверсайз'], 'cols3') }}
  </div>
  <div class=q>
   <label>Чьё мнение учитываем в стиле?</label>
   {{ chips('audience', ['только своё','партнёр','дети','коллеги','родители'], 'cols3') }}
  </div>
 </div>

 <div class="secth substep" style="margin-bottom:4px"><i>ШАГ 3</i>Пара вопросов о тебе</div>
 <p class=subcopy>По шкале: 1 — совсем не про меня, 5 — точно про меня. Это поможет собрать образы под твою натуру, а не только под внешность.</p>
 <div class=traitlist>
 {% for i, q in big5_questions %}
  <div class=traitcard>
   <p class=traitq>{{ q[2] }}</p>
   <div class=traitscale>
    {% for n in [1,2,3,4,5] %}<label class=scalechip><input type=radio name="b5_{{ i }}" value="{{ n }}"><span>{{ n }}</span></label>{% endfor %}
   </div>
  </div>
 {% endfor %}
 </div>

 <div class="secth substep" style="margin-bottom:4px"><i>ШАГ 4</i>Твой круг жизни</div>
 <p class=subcopy>Сколько примерно времени в неделю (%) занимает каждая зона. Так капсула попадёт в твою реальную жизнь, а не в абстрактный Pinterest.</p>
 <div class=lifeg>
  <label class=lifef><span class=lifev>Работа</span><input type=number name=life_work min=0 max=100 placeholder="%" class=fld></label>
  <label class=lifef><span class=lifev>Дом</span><input type=number name=life_home min=0 max=100 placeholder="%" class=fld></label>
  <label class=lifef><span class=lifev>Свободное время</span><input type=number name=life_free min=0 max=100 placeholder="%" class=fld></label>
 </div>

 <label>Бюджет на обновление гардероба (по желанию)</label>
 <select name=budget class=fld>
  <option value="">— не важно —</option>
  <option value="budget">Экономный</option>
  <option value="middle">Средний</option>
  <option value="premium">Премиум</option>
 </select>

 <label class=consent style="font-weight:normal;margin-top:22px"><input type=checkbox name=consent_processing required> Согласна на обработку данных согласно <a href="/privacy" target="_blank" rel="noopener">Политике</a>.</label>
 <label class=consent style="font-weight:normal"><input type=checkbox name=consent_transfer required> Согласна на передачу фото в ИИ-сервис для генерации образов.</label>
</div>
 <div class=submitbar><button>Собрать Карту стиля →</button></div>
 <p class=hint>Фото нужно только для генерации образов и <b>удаляется сразу после сборки</b> — храним лишь готовые образы. Нет фото под рукой? <a href="/card?text=1">Собрать пока текстовую версию</a> — образы добавишь потом, бесплатная генерация останется за тобой.</p>
</form></div></body></html>"""


WARDROBE_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Мой гардероб — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
*{box-sizing:border-box}
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf;--soft:#FBF6EC}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;margin:0;background:var(--cream);color:var(--ink);line-height:1.6}
 .wrap{max-width:940px;margin:0 auto;padding:26px 20px 80px}
 .top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}
 .logo{font-family:'Cormorant Garamond',serif;font-size:21px}
 .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:34px;margin:0 0 8px}
 .lead{color:var(--muted);font-size:15px;margin:0 0 24px;max-width:620px}
 .panel{background:#fff;border:1px solid var(--line);border-radius:18px;padding:22px 24px;margin-bottom:16px}
 .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
 .kpi{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px}
 .kpi b{display:block;font-family:'Cormorant Garamond',serif;font-size:32px;color:var(--wine);line-height:1}
 .kpi span{display:block;font-size:12.5px;color:var(--muted);margin-top:5px}
 .up{border:1.4px dashed #cdbfa6;border-radius:16px;padding:20px;background:var(--soft);text-align:center}
 .up input[type=file]{display:block;margin:10px auto;max-width:100%}
 .up button{margin-top:10px;padding:13px 26px;background:var(--wine);color:#fff;border:0;border-radius:10px;font:inherit;font-size:15px;cursor:pointer}
 .hint{font-size:12.5px;color:var(--muted);margin-top:8px}
 h2{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:23px;margin:26px 0 10px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
 .item{border:1px solid var(--line);border-radius:14px;padding:12px 14px;background:#fff}
 .item .slot{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
 .item .nm{font-size:13.5px;margin:4px 0 6px}
 .item .vd{display:inline-block;font-size:11px;padding:3px 9px;border-radius:999px}
 .keep .vd{background:#e6efe8;color:#3f7d54} .fix .vd{background:#f6eede;color:#9a6a2f}
 .drop .vd{background:#f6e7e4;color:#a5453a}
 .item .rs{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.4}
 .sug{display:flex;gap:12px;align-items:flex-start;border:1px solid rgba(93,34,48,.14);border-radius:14px;
      padding:13px 15px;background:linear-gradient(135deg,#fbf6ec,#fff);margin-bottom:9px}
 .sug b{font-size:13.5px} .sug span{display:block;font-size:12px;color:var(--muted);margin-top:3px}
 .sug .plus{flex:0 0 auto;font-size:12px;color:var(--wine);font-weight:500;white-space:nowrap}
 .empty{color:var(--muted);font-size:14px}
 .note{background:#eef6ee;border:1px solid #cfe3cf;border-radius:12px;padding:12px 15px;font-size:13.5px;color:#3a5a3a}
 @media(max-width:560px){.wrap{padding:20px 14px 70px}h1{font-size:27px}}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/cabinet">← в кабинет</a></div>
<h1>Мой гардероб</h1>
<p class=lead>Загрузи фото своих вещей — разберём каждую по твоей Формуле и покажем, сколько образов
уже собирается из того, что есть. Без единой покупки.</p>

{% if added %}<div class=note>Разобрано вещей: <b>{{ added }}</b>{% if failed and failed != '0' %} · не распознано: {{ failed }}{% endif %}.</div>{% endif %}

<div class=kpis>
 <div class=kpi><b>{{ s.looks_now }}</b><span>{{ 'образ' if s.looks_now == 1 else 'образа' if s.looks_now < 5 else 'образов' }} из твоих вещей</span></div>
 <div class=kpi><b>{{ s.keep_count }}</b><span>работают на формулу</span></div>
 <div class=kpi><b>{{ s.fix_count }}</b><span>носятся с оговоркой</span></div>
 <div class=kpi><b>{{ s.drop_count }}</b><span>не в твоей формуле</span></div>
</div>

<div class=panel>
 <form method=post action="/wardrobe/upload" enctype=multipart/form-data class=up>
  <b>Добавить вещи</b>
  <input type=file name=photos accept="image/*" multiple required>
  <button type=submit>Разобрать по Формуле</button>
  <p class=hint>До {{ limit }} фото за раз. Снимай вещь целиком на светлом фоне — так точнее.</p>
 </form>
</div>

{% if s.keep %}
<h2>Работают на формулу</h2>
<div class=grid>
 {% for it in s.keep %}
 <div class="item keep"><div class=slot>{{ it.slot }}</div><div class=nm>{{ it.name }}</div>
  <span class=vd>{{ it.bucket_label }}</span>{% if it.reason %}<div class=rs>{{ it.reason }}</div>{% endif %}</div>
 {% endfor %}
</div>
{% endif %}

{% if s.fix %}
<h2>Носятся с оговоркой</h2>
<div class=grid>
 {% for it in s.fix %}
 <div class="item fix"><div class=slot>{{ it.slot }}</div><div class=nm>{{ it.name }}</div>
  <span class=vd>{{ it.bucket_label }}</span>{% if it.reason %}<div class=rs>{{ it.reason }}</div>{% endif %}</div>
 {% endfor %}
</div>
{% endif %}

{% if s.drop %}
<h2>Не в твоей формуле</h2>
<div class=grid>
 {% for it in s.drop %}
 <div class="item drop"><div class=slot>{{ it.slot }}</div><div class=nm>{{ it.name }}</div>
  <span class=vd>{{ it.bucket_label }}</span>{% if it.reason %}<div class=rs>{{ it.reason }}</div>{% endif %}</div>
 {% endfor %}
</div>
{% endif %}

{% if suggestions %}
<h2>Чего не хватает</h2>
{% for x in suggestions %}
<div class=sug><div><b>{{ x.name }}</b><span>{{ x.why }}</span></div>
 <div class=plus>+{{ x.adds_looks }} {{ 'образ' if x.adds_looks == 1 else 'образа' if x.adds_looks < 5 else 'образов' }}</div></div>
{% endfor %}
{% elif s.total %}
<h2>Чего не хватает</h2>
<p class=empty>Ничего. Слоты капсулы закрыты — докупать нечего.</p>
{% endif %}

{% if not s.total %}
<p class=empty>Пока пусто. Загрузи первые вещи — и увидишь, что из них уже собирается.</p>
{% endif %}
</div></body></html>"""


CARD_BUILDING = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Собираем Карту стиля…</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
*{box-sizing:border-box}
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}
 .box{max-width:440px;padding:30px}
 h1{font-family:'Cormorant Garamond',serif;font-weight:normal;font-size:28px;margin:0 0 10px} p{color:var(--muted)}
 .sp{width:46px;height:46px;border:4px solid #e3dccf;border-top-color:var(--wine);border-radius:50%;margin:24px auto;animation:spin 1s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 .err{color:#9b1c1c} a{color:var(--wine)}
</style></head><body><div class=box>
<h1>Собираем твою Карту стиля</h1>
<div class=sp id=sp></div>
<p id=msg>Палитра, силуэты, 6 образов на тебе… Это занимает 1–2 минуты, не закрывай страницу.</p>
<script>
var jid="{{ job_id }}";
function poll(){
  fetch('/card/status/'+jid).then(function(r){return r.json();}).then(function(d){
    if(d.status==='done'){ location.href='/card'; }
    else if(d.status==='retry'){
      document.getElementById('sp').style.display='none';
      document.getElementById('msg').innerHTML=(d.error||'Образы пока не собрались')+'<br><br><a href="/card">Открыть сохранённую Карту</a> &nbsp; <a href="/card?rebuild=1">Повторить генерацию</a>';
    }
    else if(d.status==='stale'){
      // генерация не прошла, но готовая Карта есть — предлагаем её, не выдавая за свежую
      document.getElementById('sp').style.display='none';
      document.getElementById('msg').innerHTML=(d.error||'Сборка не завершилась')+'<br><br>Твоя предыдущая Карта на месте.<br><br><a href="/card">Открыть последнюю Карту</a> &nbsp; <a href="/card?rebuild=1">Собрать заново</a>';
    }
    else if(d.status==='error'||d.status==='unknown'){
      document.getElementById('sp').style.display='none';
      document.getElementById('msg').innerHTML='<span class=err>'+(d.error||'Сборка не завершилась')+'</span><br><br><a href="/card?rebuild=1">Попробовать снова</a>';
    } else { setTimeout(poll, 4000); }
  }).catch(function(){ setTimeout(poll, 4000); });
}
setTimeout(poll, 3000);
</script>
</div></body></html>"""


STYLIST_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Стилист — Чувство стиля</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600&family=Onest:wght@300;400;500&display=swap" rel=stylesheet>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);height:100vh;display:flex;flex-direction:column}
 .top{display:flex;justify-content:space-between;align-items:center;padding:14px 20px;border-bottom:1px solid var(--line);background:#fff}
 .top .logo{font-size:17px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .feed{flex:1;overflow-y:auto;padding:20px;max-width:720px;width:100%;margin:0 auto}
 .msg{margin:10px 0;display:flex} .msg .b{padding:11px 15px;border-radius:14px;font-size:15.5px;line-height:1.5;max-width:80%}
 .msg.u{justify-content:flex-end} .msg.u .b{background:var(--wine);color:#fff;border-bottom-right-radius:4px}
 .msg.a .b{background:#fff;border:1px solid var(--line);border-bottom-left-radius:4px}
 .typing{color:var(--muted);font-size:14px;padding:0 4px}
 .bar{border-top:1px solid var(--line);background:#fff;padding:12px 16px}
 .bar .in{max-width:720px;margin:0 auto;display:flex;gap:10px}
 textarea{flex:1;resize:none;border:1px solid #d9d2c7;border-radius:10px;padding:11px 13px;font:inherit;font-size:15px;height:46px;max-height:120px}
 .bar button{background:var(--wine);color:#fff;border:0;border-radius:10px;padding:0 20px;font:inherit;font-size:15px;cursor:pointer}
 .hint{max-width:720px;margin:8px auto 0;color:var(--muted);font-size:12px;text-align:center}
</style></head><body>
<div class=top><span class=logo>Чувство стиля · стилист</span><a href="/me">← профиль</a></div>
<div class=feed id=feed></div>
<div class=bar><div class=in>
 <textarea id=inp placeholder="Спроси: что надеть на встречу? идёт ли мне это пальто? с чего начать?"></textarea>
 <button id=send onclick=sendMsg()>→</button>
</div><div class=hint>Стилист опирается на твою Формулу стиля и методологию. Не виртуальная примерка — живой совет.</div></div>
<script>
var history=[];
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function add(role,text){var f=document.getElementById('feed');var d=document.createElement('div');
 d.className='msg '+(role==='user'?'u':'a');d.innerHTML='<div class=b>'+esc(text).replace(/\\n/g,'<br>')+'</div>';
 f.appendChild(d);f.scrollTop=f.scrollHeight;return d;}
function sendMsg(){var inp=document.getElementById('inp');var t=inp.value.trim();if(!t)return;
 inp.value='';add('user',t);history.push({role:'user',content:t});
 var tip=document.createElement('div');tip.className='msg a';tip.innerHTML='<div class="b typing">печатает…</div>';
 document.getElementById('feed').appendChild(tip);
 fetch('/stylist/msg',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:history})})
  .then(function(r){return r.json();}).then(function(d){tip.remove();
   var rep=d.reply||'Не получилось ответить, попробуй ещё раз.';add('assistant',rep);history.push({role:'assistant',content:rep});})
  .catch(function(){tip.remove();add('assistant','Связь прервалась — попробуй ещё раз.');});}
document.getElementById('inp').addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}});
add('assistant','Привет. Я твой стилист. Помогу одеваться так, чтобы тебя считывали той, кем ты себя ощущаешь — а не той, кем привыкли видеть. С чем хочешь разобраться?');
</script>
</body></html>"""


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


@app.get("/")
def landing():
    return send_from_directory(str(WEB_DIR), "index.html")


def _safe_next(url: str | None) -> str | None:
    """Локальный путь для редиректа после входа (защита от open redirect)."""
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return None


@app.get("/demo")
def demo():
    # Единая диагностика — это КВИЗ. /demo больше не дублирует форму, ведём на квиз.
    return redirect("/identity-scan-quiz.html?fresh=1")


@app.get("/quiz")
def quiz_short():
    # Короткий путь для статей/воронки (cta_url из «Скилла статей») → диагностика-квиз.
    return redirect("/identity-scan-quiz.html?fresh=1")


def _user_stage() -> str:
    """Где человек стоит на пути: nothing → diagnosed → carded.

    Тарифные кнопки лендинга статические и раньше вели прямо в продукт. Человек с уже пройденным
    квизом упирался в экран «сначала диагностика», хотя диагностика у него есть: путь должен
    открывать то, что уже заслужено, а не начинаться заново.
    """
    user = _current_user()
    _attach_quiz_diagnosis(user)  # диагноз квиза лежит под job_id — привязываем к пользователю
    prof = get_profile(user)
    if prof.get("card"):
        return "carded"
    if (prof.get("diagnosis") or {}).get("style_formula"):
        return "diagnosed"
    return "nothing"


@app.get("/start/card")
def start_card():
    """Кнопка тарифа «Карта стиля» → туда, где человек уже находится."""
    stage = _user_stage()
    record_event("tier_click_card", _current_user(), meta=stage)
    # Карта есть или есть диагностика — открываем Карту (во втором случае она соберётся).
    # Ничего нет — вести в продукт незачем: там всё равно нечего показать, идём в диагностику.
    return redirect("/card" if stage in ("carded", "diagnosed") else "/quiz")


@app.get("/start/daily")
def start_daily():
    """Кнопка тарифа «Стиль каждый день» → кабинет, Карта или диагностика, по состоянию."""
    stage = _user_stage()
    record_event("tier_click_daily", _current_user(), meta=stage)
    # Кабинет продолжает Карту, поэтому без Карты ведём сначала собрать её.
    return redirect({"carded": "/cabinet", "diagnosed": "/card"}.get(stage, "/quiz"))


@app.get("/privacy")
def privacy():
    return render_template_string(PRIVACY)


# ── Блог /blog — дом контента и SEO; статьи из content/blog/*.md ──────────────────
_BLOG_DIR = Path(__file__).resolve().parent.parent / "content" / "blog"

_BLOG_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400;1,500&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">')

_BLOG_CSS = (
    ":root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf;--ph:#e9e0d0}"
    "*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font-family:Onest,-apple-system,Segoe UI,sans-serif;line-height:1.6}"
    ".wrap{max-width:1120px;margin:0 auto;padding:30px 26px 110px}.artwrap{max-width:680px}"
    ".top{display:flex;justify-content:space-between;align-items:center}.logo{font-family:'Cormorant Garamond',serif;font-size:20px}.top a{color:var(--muted);font-size:14px;text-decoration:none}"
    ".eyebrow{font-size:11px;letter-spacing:.26em;text-transform:uppercase;color:var(--wine);margin:44px 0 12px}"
    "h1.jt{font-family:'Cormorant Garamond',serif;font-weight:500;font-size:56px;line-height:1.02;margin:0 0 14px;letter-spacing:-.01em}"
    ".lead{color:var(--muted);font-size:18px;margin:0 0 46px;max-width:560px}"
    ".chip{display:inline-block;font-size:10.5px;letter-spacing:.18em;text-transform:uppercase;color:var(--wine);margin-bottom:12px}"
    # featured: крупный образ, минимум текста
    ".feat{display:block;text-decoration:none;color:inherit;margin-top:6px}"
    ".feat .cov{aspect-ratio:16/9;background-size:cover;background-position:center;background-color:var(--ph);border-radius:8px}"
    ".feat .body{padding:24px 4px 0;max-width:760px}.feat h2{font-family:'Cormorant Garamond',serif;font-weight:500;font-size:38px;line-height:1.06;margin:0 0 10px}.feat p{color:var(--muted);font-size:17px;margin:0}"
    # сетка карточек: образ доминирует
    ".grid{display:grid;grid-template-columns:1fr 1fr;gap:20px 30px;margin-top:64px}"
    "@media(max-width:720px){.grid{grid-template-columns:1fr;gap:44px}h1.jt{font-size:40px}.feat h2{font-size:28px}}"
    ".card{display:block;text-decoration:none;color:inherit}"
    ".card .cov{aspect-ratio:4/5;background-size:cover;background-position:center;background-color:var(--ph);border-radius:8px;transition:opacity .2s}"
    ".card:hover .cov{opacity:.92}"
    ".card .body{padding:16px 2px 0}.card h3{font-family:'Cormorant Garamond',serif;font-weight:500;font-size:23px;line-height:1.12;margin:0 0 6px}.card p{color:var(--muted);font-size:15px;margin:0}"
    ".empty{padding:40px 2px;margin-top:18px;color:var(--muted);font-size:18px}"
    # статья: редакционная подача
    ".hero{aspect-ratio:3/2;background-size:cover;background-position:center;background-color:var(--ph);border-radius:10px;margin:10px 0 40px}"
    "article{font-size:19px;line-height:1.85}"
    "article>p:first-of-type{font-size:23px;line-height:1.5;color:#2a2620;margin:0 0 30px}"
    "article h2{font-family:'Cormorant Garamond',serif;font-weight:500;font-size:30px;line-height:1.15;margin:46px 0 16px}"
    "article p{margin:0 0 26px}article ul{padding-left:22px;margin:0 0 26px}article li{margin:9px 0}article a{color:var(--wine)}"
    "article img{width:100%;border-radius:10px;margin:20px 0}"
    "article blockquote{font-family:'Cormorant Garamond',serif;font-style:italic;font-size:27px;line-height:1.35;color:var(--wine);margin:40px 0;padding:0;border:0}"
    ".meta{font-size:12.5px;color:#a89f92;letter-spacing:.08em;text-transform:uppercase;margin:0 0 24px}"
    ".cta{border-top:1px solid var(--line);padding-top:34px;margin-top:56px;text-align:center;font-size:18px}"
    ".cta a{display:inline-block;margin-top:16px;background:var(--wine);color:#fff;padding:15px 34px;border-radius:999px;text-decoration:none;font-size:15px}"
)

BLOG_INDEX = ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
    "<meta name=viewport content=\"width=device-width, initial-scale=1\"><title>Журнал — Чувство стиля</title>"
    "<meta name=description content=\"Журнал «Чувство стиля»: психология стиля, цвет, силуэт, капсула, образ под новую роль.\">"
    + _BLOG_FONTS + "<style>" + _BLOG_CSS + "</style></head><body><div class=wrap>"
    "<div class=top><span class=logo>Чувство стиля</span><a href=\"/\">← на главную</a></div>"
    "<div class=eyebrow>Журнал</div><h1 class=jt>Психология стиля</h1>"
    "<p class=lead>Не про тренды. Про то, как образ совпадает с тем, кем ты стала: цвет, силуэт, капсула, роль.</p>"
    "{% if articles %}{% set f = articles[0] %}"
    "<a class=feat href=\"/blog/{{ f.slug }}\"><div class=cov style=\"{% if f.cover %}background-image:url('{{ f.cover }}'){% endif %}\"></div>"
    "<div class=body>{% if f.category %}<span class=chip>{{ f.category }}</span>{% endif %}<h2>{{ f.title }}</h2>{% if f.description %}<p>{{ f.description }}</p>{% endif %}</div></a>"
    "{% if articles[1:] %}<div class=grid>{% for a in articles[1:] %}"
    "<a class=card href=\"/blog/{{ a.slug }}\"><div class=cov style=\"{% if a.cover %}background-image:url('{{ a.cover }}'){% endif %}\"></div>"
    "<div class=body>{% if a.category %}<span class=chip>{{ a.category }}</span>{% endif %}<h3>{{ a.title }}</h3>{% if a.description %}<p>{{ a.description }}</p>{% endif %}</div></a>"
    "{% endfor %}</div>{% endif %}"
    "{% else %}<div class=empty>Здесь скоро появятся статьи. А пока — пройди бесплатную диагностику и узнай свой стилевой разрыв.<br><br>"
    "<a href=\"/quiz\" style=\"color:var(--wine)\">Пройти диагностику →</a></div>{% endif %}"
    "</div></body></html>")

BLOG_ARTICLE = ("<!doctype html><html lang=ru><head><meta charset=utf-8>"
    "<meta name=viewport content=\"width=device-width, initial-scale=1\"><title>{{ a.title }} — Чувство стиля</title>"
    "{% if a.description %}<meta name=description content=\"{{ a.description }}\">{% endif %}"
    + _BLOG_FONTS + "<style>" + _BLOG_CSS + "</style></head><body><div class=\"wrap artwrap\">"
    "<div class=top><span class=logo>Чувство стиля</span><a href=\"/blog\">← журнал</a></div>"
    "<div class=eyebrow>{% if a.category %}{{ a.category }}{% else %}Журнал{% endif %}</div><h1 class=jt>{{ a.title }}</h1>"
    "{% if a.date %}<p class=meta>{{ a.date }}</p>{% endif %}"
    "{% if a.cover %}<div class=hero style=\"background-image:url('{{ a.cover }}')\"></div>{% endif %}"
    "<article>{{ a.html|safe }}</article>"
    "<div class=cta>Хочешь понять, как твой образ считывается сейчас, и собрать стиль под новый этап?"
    "<br><a href=\"/quiz\">Пройти бесплатную диагностику</a></div>"
    "</div></body></html>")


def _render_markdown(text: str) -> str:
    """Markdown → HTML. Есть пакет markdown — полный рендер; нет — минимальный фолбэк (прод не падает)."""
    try:
        import markdown as _md  # type: ignore
        return _md.markdown(text, extensions=["extra", "sane_lists"])
    except Exception:  # noqa: BLE001 — фолбэк без зависимости
        import html as _h
        out = []
        for block in text.split("\n\n"):
            b = block.strip()
            if not b:
                continue
            img = re.match(r"!\[(.*?)\]\((.+?)\)$", b)
            if b.startswith("## ") or b.startswith("# "):
                out.append("<h2>" + _h.escape(b.lstrip("# ")) + "</h2>")
            elif img:
                out.append('<img src="%s" alt="%s">' % (img.group(2), _h.escape(img.group(1))))
            elif b.startswith("> "):
                q = " ".join(l[2:] if l.startswith("> ") else l for l in b.splitlines())
                out.append("<blockquote>" + _h.escape(q) + "</blockquote>")
            elif b.startswith("- "):
                items = "".join("<li>" + _h.escape(l[2:]) + "</li>"
                                for l in b.splitlines() if l.startswith("- "))
                out.append("<ul>" + items + "</ul>")
            else:
                p = _h.escape(b)
                p = re.sub(r"!\[(.*?)\]\((.+?)\)", r'<img src="\2" alt="\1">', p)
                p = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", p)
                p = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', p)
                out.append("<p>" + p.replace("\n", "<br>") + "</p>")
        return "\n".join(out)


def _load_articles() -> list:
    """Статьи из content/blog/*.md (frontmatter title/description/date/slug). Новые сверху."""
    arts = []
    if not _BLOG_DIR.exists():
        return arts
    for f in sorted(_BLOG_DIR.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        try:
            raw = f.read_text(encoding="utf-8")
        except OSError:
            continue
        meta, body = {}, raw
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                for line in raw[3:end].splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip()] = v.strip().strip('"')
                body = raw[end + 4:].lstrip("\n")
        arts.append({"slug": meta.get("slug") or f.stem, "title": meta.get("title") or f.stem,
                     "description": meta.get("description", ""), "date": meta.get("date", ""),
                     "category": meta.get("category", ""), "cover": meta.get("cover", ""), "body": body})
    arts.sort(key=lambda a: a.get("date", ""), reverse=True)
    return arts


@app.get("/blog")
def blog_index():
    return render_template_string(BLOG_INDEX, articles=_load_articles())


@app.get("/blog/<slug>")
def blog_article(slug):
    for a in _load_articles():
        if a["slug"] == slug:
            a = dict(a, html=_render_markdown(a["body"]))
            return render_template_string(BLOG_ARTICLE, a=a)
    return redirect("/blog")


@app.get("/login")
def login():
    nxt = _safe_next(request.args.get("next"))
    if session.get("email"):
        return redirect(nxt or "/me")
    return render_template_string(LOGIN_PAGE, error=None, sent=False, email="",
                                  dev_link=None, next=nxt or "")


@app.post("/login")
def login_send():
    email = (request.form.get("email") or "").strip()
    nxt = _safe_next(request.form.get("next"))
    if "@" not in email or "." not in email:
        return render_template_string(LOGIN_PAGE, error="Введи корректный email.",
                                      sent=False, email=email, dev_link=None, next=nxt or ""), 400
    if nxt:
        session["next_url"] = nxt  # вернёмся сюда после клика по ссылке входа
    if request.form.get("marketing"):  # согласие на рассылку → для выгрузки в UniSender
        record_event("marketing_optin", email.lower())
    link = request.url_root.rstrip("/") + "/auth?token=" + make_token(email)
    # режим теста: не гоняем клиентку в почту — впускаем сразу, письмо шлём фоном (для возврата)
    if _open_access():
        session["email"] = email
        threading.Thread(target=send_magic_link, args=(email, link), daemon=True).start()
        return redirect(nxt or "/card")
    sent = send_magic_link(email, link)
    # БЕЗОПАСНОСТЬ: ссылка входа = рабочий токен. НЕ показываем её на экране для произвольной почты
    # (иначе любой ввёл бы чужой email и вошёл в её аккаунт — захват аккаунта). Показываем ТОЛЬКО:
    # админу (самотест фаундера) или в локальном dev по флагу SENSE_DEV_LINK. На публичном проде без
    # настроенной почты вход не сработает — это сигнал задать UNISENDER_API_KEY/FROM, а не дыра.
    dev_ok = email.lower() in _ADMIN_EMAILS or os.getenv("SENSE_DEV_LINK") == "1"
    return render_template_string(LOGIN_PAGE, error=None, sent=True, email=email,
                                  dev_link=(None if sent else (link if dev_ok else None)), next=nxt or "")


@app.get("/auth")
def auth_verify():
    email = read_token(request.args.get("token") or "")
    if not email:
        return render_template_string(
            LOGIN_PAGE, error="Ссылка недействительна или устарела — запроси новую.",
            sent=False, email="", dev_link=None, next=""), 400
    anon = session.get("anon")
    if anon:
        merge_profile(anon, email)   # анонимная сессия не должна пропадать при входе
    session["email"] = email
    session.permanent = True
    return redirect(_safe_next(session.pop("next_url", None)) or "/me")


@app.get("/logout")
def logout():
    session.pop("email", None)
    return redirect("/")


_RU_MON = ["янв", "фев", "мар", "апр", "май", "июн",
           "июл", "авг", "сен", "окт", "ноя", "дек"]


def _ru_date(ts: str) -> str:
    """ISO-время → «2 июл 2026» для трекера. Битый ts → первые 10 символов."""
    try:
        d = datetime.fromisoformat(ts)
        return f"{d.day} {_RU_MON[d.month - 1]} {d.year}"
    except (ValueError, TypeError):
        return (ts or "")[:10]


@app.get("/me")
def me():
    email = _current_user()
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    track = gap_progress(email)  # трекер разрыва: точки-замеры + дельта только при ≥2 замерах
    if track:  # человекочитаемые даты точек (точка отсчёта — первая)
        for p in track["points"]:
            p["date"] = _ru_date(p["ts"])
    return render_template_string(
        ME_PAGE, email=_display_name(email), has_diag=bool(diag.get("style_formula")),
        formula=diag.get("style_formula", ""),
        has_style=bool(prof.get("style_profile")), track=track,
    )


_CATALOG_CACHE: list = []
_BRAND_STYLES_CACHE: dict = {}


def _brand_styles() -> dict:
    """Бренд (в нижнем регистре) → стилевые поля метода (classic/natural/drama/romance)
    из data/fashion-base/brands.csv. Чтобы вещь наследовала стиль своего бренда (офлайн-разметка)."""
    global _BRAND_STYLES_CACHE
    if _BRAND_STYLES_CACHE:
        return _BRAND_STYLES_CACHE
    fp = Path(__file__).resolve().parent.parent / "data" / "fashion-base" / "brands.csv"
    out: dict = {}
    if fp.exists():
        import csv as _csv
        try:
            for r in _csv.DictReader(fp.open(encoding="utf-8-sig")):
                name = (r.get("brand_name") or "").strip().lower()
                if name:
                    out[name] = (r.get("style_fields") or "").strip()
        except Exception:  # noqa: BLE001 — разметка не должна ронять кабинет
            pass
    _BRAND_STYLES_CACHE = out
    return out


def _catalog_products() -> list:
    """Реальные вещи брендов (Ushatava/Lichi) с фото и партнёрскими ссылками — кэш в памяти.
    Каждой вещи проставляем стиль её бренда (из brands.csv) для подбора под подстиль."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE:
        return _CATALOG_CACHE
    base = Path(__file__).resolve().parent.parent / "data" / "fashion-base"
    brand_styles = _brand_styles()
    prods: list = []
    # Только брендовые фиды со студийной съёмкой. products_wb.csv (маркетплейс) отключён
    # сознательно: там на фото нарисован рекламный текст поверх вещи — «ТРЕНД 2026», логотипы
    # магазинов, коллажи «мега вместительная». По типу изображения это честный packshot, и
    # отличить его нельзя, а клиентка видит чужие картинки вместо своей капсулы.
    # Цена: слот «Обувь» остаётся без фото — обуви в брендовых фидах нет вовсе. Это осознанный
    # размен: обувь называется в составе образа текстом, но чужого фото под неё не подставляем.
    # Вернуть маркетплейс можно через SENSE_CATALOG_WB=1, когда появится своя база с фото.
    sources = ["products_ushatava.csv", "products_lichi.csv"]
    if os.getenv("SENSE_CATALOG_WB") == "1":
        sources.append("products_wb.csv")
    for fname in sources:
        fp = base / fname
        if fp.exists():
            try:
                for p in parse_csv(fp):
                    if not p.style_fields:
                        p.style_fields = brand_styles.get((p.brand or "").strip().lower(), "")
                    prods.append(p)
            except Exception:  # noqa: BLE001 — каталог не должен ронять кабинет
                pass
    _CATALOG_CACHE = prods
    return prods


# Вещи, которых не может быть в ядре капсулы: бельё, домашнее, пляжное, «расходники». Фиды брендов
# отдают их вперемешку с одеждой, и в капсулу прилетали «Подъюбник из вискозы» в слот Низ и «Топ-бра»
# в слот Верх. По канону расходники в счёт вещей капсулы не входят.
_CAPSULE_EXCLUDE = ("подъюбник", "пижам", "бра", "бюстгальтер", "трус", "бельё", "белье",
                    "купальник", "плавк", "чулк", "носки", "колготк", "халат", "сорочка ночная",
                    # пляжное и домашнее — не капсульный гардероб: «Рубашка летняя прозрачная для
                    # пляжа» приходила клиентке в рабочую капсулу как обычный верх
                    "для пляжа", "пляжн", "парео", "домашн", "спальн")

# Устаревшее по курсу «Алгоритмы имиджа» + подиумам SS26/FW26-27 (см. architecture/trends-2026-2027.md).
# Каталог отдаёт всё подряд, включая вещи, которые читаются как «немодно», и они попадали в капсулу
# наравне с актуальными: клиентка видела скинни, длинные угги и микро-сумки в «своей» подборке.
_DATED_ITEMS = (
    "скинни", "skinny",                      # ушли из базы
    "рукав 3/4", "рукав ¾",                  # устарел ещё в 2017-18
    "тедди",                                 # шубы-тедди
    "авиатор",                               # дублёнка-авиаторка
    "бананк", "поясная сумка",               # перегружает талию
    "микро-сумк", "мини-рюкзак", "микрорюкзак",
    "рванк", "потёртост", "потертост", "стразы", "бахром",  # деним с декором
    "помпон", "заклёпк", "заклепк",
)


def _is_dated(name: str) -> bool:
    """Вещь читается как устаревшая — в капсулу не берём, даже если она подходит по цвету."""
    n = (name or "").lower()
    return any(k in n for k in _DATED_ITEMS)

# Сезонная несовместимость: капсула собирается на сезон (card["season"] = ss|fw), а каталог о
# сезоне не знает. Без этого в капсулу «Осень–зима» падали летний лён и пляжные рубашки.
_SEASON_WRONG = {
    # Списки пополнены по диффу капсулы: причина «не по сезону» обязана быть точной, иначе
    # клиентка видит «уступила место» там, где вещь ушла из-за погоды. Босоножки зимой и
    # пуховик летом — это сезон, а не конкуренция за слот.
    "fw": ("летн", "пляжн", "для пляжа", "льнян", "изо льна", "из льна", "сарафан", "шорты",
           "босонож", "сандал", "шлёпан", "шлепан", "вьетнамк", "на бретелях", "майка",
           "лёгкий плащ", "легкий плащ"),
    "ss": ("пуховик", "шуба", "дублён", "дублен", "зимн", "утеплён", "утеплен", "угги",
           "шерстян", "кашемир", "пальто", "тёплый свитер", "теплый свитер", "водолазк",
           "сапоги", "ботильон", "мех"),
}


_SPORTY_SHOES = ("кроссовк", "кед", "слипон", "сникер", "дутик", "шлепанц", "сланц")


def _is_sporty_shoe(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _SPORTY_SHOES)


# Сезон в продукте живёт в двух видах: коды капсулы (ss/fw) и человеческие имена из интерфейса
# (?season=summer в кабинете, radio-кнопки формы). Словари выше ключуются кодами, поэтому
# «summer» в них просто не находился и сезонный фильтр молча отключался целиком: в летнюю
# капсулу спокойно падали угги, пальто и зимняя водолазка.
_SEASON_CODE = {"summer": "ss", "spring": "ss", "autumn": "fw", "fall": "fw", "winter": "fw",
                "ss": "ss", "fw": "fw"}


def season_code(season: str | None) -> str:
    """Любое написание сезона → код капсулы ss/fw. Неизвестное — пустая строка (фильтр не душим)."""
    return _SEASON_CODE.get((season or "").strip().lower(), "")


def _season_ok(name: str, season: str | None) -> bool:
    """Вещь уместна в сезоне капсулы. Мягкий фильтр по названию — в фидах сезон отдельным полем
    не приходит."""
    bad = _SEASON_WRONG.get(season_code(season))
    if not bad:
        return True
    n = (name or "").lower()
    return not any(k in n for k in bad)


def _is_capsule_worthy(name: str) -> bool:
    n = (name or "").lower()
    return not any(k in n for k in _CAPSULE_EXCLUDE)


# Русификация названий вещей каталога для показа клиентке. Фиды брендов зовут фасоны по-английски
# («Bootcut», «Baggy», «Grandpa Fit», «Dropped Shoulder»), а имена коллекций («Nixie», «Dachi»)
# клиентке ничего не говорят. Переводим фасоны, вычищаем коллекции. Ссылку на покупку НЕ трогаем —
# в магазине товар остаётся под своим именем.
_NAME_GLOSS = [
    ("dropped shoulder", "со спущенным плечом"), ("grandpa fit", "свободного кроя"),
    ("wide leg", "широкие"), ("a-line", "а-силуэт"), ("boot cut", "клёш от колена"),
    ("bootcut", "клёш от колена"), ("baggy", "свободные"), ("straight", "прямые"),
    ("regular", "прямые"), ("skinny", "узкие"), ("slim", "зауженные"),
    ("oversized", "объёмный"), ("oversize", "объёмный"), ("cropped", "укороченный"),
    ("crop", "укороченный"), ("flared", "клёш"), ("flare", "клёш"), ("palazzo", "палаццо"),
    ("mom", "мом"), ("total", "тотал"), ("basic", "базовый"),
    ("midi", "миди"), ("maxi", "макси"), ("mini", "мини"),
]


def _ru_item_name(name: str) -> str:
    """EN-фасоны → русский; имена коллекций (латиница без перевода) вычищаем. Для показа, не для ссылки."""
    import re
    s = name or ""
    for en, ru in _NAME_GLOSS:
        s = re.sub(r"(?i)\b" + re.escape(en) + r"\b", ru, s)
    stripped = re.sub(r"\s*\b[A-Za-z][A-Za-z'’\d]*\b\s*", " ", s)  # оставшаяся латиница = коллекция
    if re.search(r"[А-Яа-я]", stripped):  # но не опустошаем имя целиком
        s = stripped
    s = re.sub(r"\s+", " ", s).strip(" -,")
    return (s[0].upper() + s[1:]) if s else (name or "")


def _capsule_quota(n: int) -> dict:
    """Сколько вещей брать в каждый слот. Канон «Алгоритмы имиджа»: верхов ВСЕГДА больше, чем низов
    (капсула богатеет за счёт верхов), верхний слой — 1-2, платье — не ядро капсулы (низкая
    комбинаторика). Без квот отбор шёл просто по релевантности и выдавал 4 жакета и 3 платья
    на 2 верха — набор вещей, а не капсула."""
    if n <= 6:
        return {"Верхний слой": 1, "Верх": 2, "Низ": 1, "Обувь": 1, "Аксессуары": 1}
    # 9 — стартовая капсула Карты, ровно столько обещано в тарифе. Раньше этой ступени не было:
    # любое n>6 давало 12, и клиентка в публичной Карте получала не то число, что прочитала на лендинге.
    if n <= 9:
        return {"Верхний слой": 1, "Верх": 3, "Низ": 2, "Платья и комбинезоны": 1,
                "Обувь": 1, "Аксессуары": 1}
    return {"Верхний слой": 1, "Верх": 4, "Низ": 3, "Платья и комбинезоны": 1,
            "Обувь": 2, "Аксессуары": 1}


def _dedup_products(items: list) -> list:
    """Убрать повторы одной и той же вещи. В фидах брендов один товар приходит несколько раз
    (разные цвета/строки): «Приталенный однобортный жакет» ×3, «Расклешенные брюки» ×3 — и все
    они попадали в капсулу как разные вещи. Ключ — нормализованное имя; среди дублей побеждает
    packshot (предметное фото), а не съёмка на модели."""
    best: dict[str, object] = {}
    for p in items:
        key = " ".join((p.name or "").lower().split())
        if not key:
            continue
        cur = best.get(key)
        if cur is None or ((p.image_kind or "") == "packshot"
                           and (getattr(cur, "image_kind", "") or "") != "packshot"):
            best[key] = p
    return list(best.values())


# Фиды с чистой студийной съёмкой. Всё остальное (маркетплейсы) — только когда в слоте нет
# альтернативы: например обуви в брендовых фидах нет вовсе, и без них слот остался бы пустым.
_CLEAN_SOURCES = ("lichi", "ushatava")


def _is_clean_source(product) -> bool:
    brand = (getattr(product, "brand", "") or "").strip().lower()
    return any(src in brand for src in _CLEAN_SOURCES)


def _visual_capsule(card: dict, diag: dict, n: int) -> list:
    """Визуальная капсула для конструктора: реальные вещи каталога, подобранные под Формулу
    (палитра/табу/фигура), сгруппированные по слотам. Каждая вещь — с фото и ссылкой купить.
    Структура капсулы — по квотам слотов (см. _capsule_quota), а не «топ-N по релевантности»."""
    products = _catalog_products()
    if not products:
        return []
    # доминанты стиля клиентки: топ-2 поля из семантики диагностики (classic/natural/drama/romance)
    dist = diag.get("semantic_field_distribution") or {}
    styles = [k for k, _ in sorted(dist.items(), key=lambda kv: kv[1] or 0, reverse=True)
              if (dist.get(k) or 0) > 0][:2]
    # Стоп-цвета. Генератор палитры кладёт их в card["stop_colors"] (см. pipeline.generate_card_palette),
    # а здесь годами читалось card["stop_list"] — поля, которого в Карте нет. Табу-цвета просто не
    # доезжали до фильтра: клиентке мягкого лета в капсулу спокойно падал чистый чёрный.
    # Берём оба имени плюс stop_list из visual_formula диагностики.
    stop_colors = (card.get("stop_colors") or card.get("stop_list")
                   or ((diag.get("visual_formula") or {}).get("stop_list")) or [])
    profile = {
        "palette": card.get("palette") or [],
        "stop_list": stop_colors,
        "figure_type": diag.get("figure_type"),
        "colortype": diag.get("colortype"),
        "season": card.get("season"),
        "base_style": (diag.get("style_dominant") or diag.get("base_style") or ""),
        "styles": styles,
        "gender": "женский",
    }
    # Ранжируем ВЕСЬ каталог под профиль, чтобы в каждом слоте был выбор, и раскладываем по слотам
    # (порядок внутри слота = релевантность). Предметное фото вперёд: в капсуле нужна сама вещь.
    scored = score_products(profile, products)
    # Порог: вещь, у которой И цвет вне палитры, И стиль мимо Формулы, уходит в минус — такую в
    # капсулу не берём. Отсекаем только пока есть из чего выбирать: если после отсева слот пуст,
    # лучше показать слабую вещь, чем пустой слот (проверяется ниже, при доборе).
    good = [(s, p) for s, p in scored if s > 0]
    ranked = _dedup_products([p for _, p in (good if len(good) >= n * 3 else scored)])
    season = (card.get("season") or "").lower()
    ranked = [p for p in ranked
              if _is_capsule_worthy(p.name) and not _is_dated(p.name)
              and _season_ok(f"{p.name} {p.category}", season)]
    by_slot: dict[str, list] = {}
    rank = {id(p): i for i, p in enumerate(ranked)}  # позиция = релевантность профилю
    for p in ranked:
        # Имя ВПЕРЕДИ категории: категории фидов врут. У «Пальто свободного демисезонного с поясом»
        # категория в фиде WB — «аксессуар», и пальто уезжало в слот сумок и ремней.
        by_slot.setdefault(_capsule_slot(p.name, p.category), []).append(p)
    # Предметное фото приятнее в конструкторе, но раньше packshot сортировался ГЛАВНЫМ ключом и
    # перекрывал релевантность: нерелевантный packshot вставал впереди подходящей вещи на модели —
    # так в капсулу «Классики» попадали кроссовки. Теперь это фора в несколько позиций, не приоритет.
    _PACKSHOT_BONUS = 8
    # Брендовые фиды идут вперёд маркетплейсных. На фото маркетплейса часто нарисован рекламный
    # текст поверх вещи («ТРЕНД 2026», логотип магазина, коллаж «мега вместительная») — по типу
    # изображения это честный packshot, и никакой фильтр его не отличит. У брендовых фидов
    # съёмка студийная и чистая, поэтому решаем источником, а не попыткой распознать картинку.
    _BRAND_BONUS = 40
    for slot in by_slot:
        by_slot[slot].sort(key=lambda p: rank[id(p)]
                           - (_PACKSHOT_BONUS if (p.image_kind or "") == "packshot" else 0)
                           - (_BRAND_BONUS if _is_clean_source(p) else 0))
    # Обувь: кроссовки — не база капсулы. Клиентке-классике доставались две пары кроссовок, хотя в
    # каталоге десятки лоферов. Спортивную опускаем в конец слота: она возьмётся, только если
    # неспортивной обуви под её палитру не нашлось вовсе.
    shoes = by_slot.get("Обувь") or []
    if any(_is_sporty_shoe(p.name) for p in shoes):
        by_slot["Обувь"] = ([p for p in shoes if not _is_sporty_shoe(p.name)]
                            + [p for p in shoes if _is_sporty_shoe(p.name)])

    quota = _capsule_quota(n)
    picked: dict[str, list] = {s: by_slot.get(s, [])[:q] for s, q in quota.items() if by_slot.get(s)}
    # Слот оказался беднее квоты (маленький каталог) → добираем верхами и низами, но НЕ ломая
    # правило «верхов больше, чем низов».
    total = sum(len(v) for v in picked.values())
    for slot in ("Верх", "Низ", "Верхний слой", "Аксессуары"):
        while total < n:
            rest = [p for p in by_slot.get(slot, []) if p not in picked.get(slot, [])]
            if not rest:
                break
            if slot == "Низ" and len(picked.get("Низ", [])) + 1 >= len(picked.get("Верх", [])):
                break
            picked.setdefault(slot, []).append(rest[0])
            total += 1

    order = [s for s, _ in _CAPSULE_SLOTS] + [_SLOT_OTHER]
    return [{"slot": s, "items": [{"name": _ru_item_name(p.name), "image": p.image, "url": p.url, "brand": p.brand,
                                   "price": int(p.price) if p.price else None, "image_kind": p.image_kind} for p in picked[s]]}
            for s in order if picked.get(s)]


def _inline_capsule_images(board: list, max_side: int = 640, timeout: float = 4.0) -> list:
    """Скачать фото вещей и вшить как data-URL. Нужно для Карты/PDF: html2pdf не тянет внешние
    CDN брендов (CORS) → в PDF были бы пустые рамки. Параллельно, с таймаутом; при сбое вещь
    остаётся без фото (шаблон покажет её текстом). Мутирует board на месте и возвращает его."""
    items = [it for grp in board for it in grp.get("items", [])]

    def _fetch(it: dict) -> None:
        url = it.get("image") or ""
        if not url.startswith("http"):
            return
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            img = Image.open(io.BytesIO(r.content)).convert("RGB")
            w, h = img.size
            scale = min(1.0, max_side / max(w, h))
            if scale < 1.0:
                img = img.resize((round(w * scale), round(h * scale)))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            it["image"] = "data:image/jpeg;base64," + base64.standard_b64encode(buf.getvalue()).decode()
        except Exception:  # noqa: BLE001 — фото не тянется → вещь остаётся текстом
            it["image"] = None

    if items:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(items))) as ex:
            list(ex.map(_fetch, items))
    return board


CABINET_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Стиль каждый день</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 /* ── Кабинет «Стиль каждый день»: тот же дашборд-каркас, что и Карта (макет 19.07.2026).
    Карта отвечает на «кто я по стилю», кабинет — на «что мне делать с этим сегодня»: слева
    конструктор капсулы, справа образ на сегодня, ниже — AI-помощник и докупки. #2 тариф
    ничего не пересобирает: он берёт формулу и ядро из Карты и применяет их. */
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--wine2:#7A2438;--muted:#6b645c;
       --line:#e6dfd2;--sand:#F3ECDF;--soft:#FBF6EC;--violet:#5B4B8A}
 *{box-sizing:border-box}
 body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;margin:0;
      background:var(--cream);color:var(--ink);line-height:1.55;-webkit-font-smoothing:antialiased}
 .shell{display:grid;grid-template-columns:228px 1fr;min-height:100vh}

 /* ── левая колонка (общий каркас с Картой) ───────────────────────────────────────────── */
 .side{background:var(--sand);border-right:1px solid var(--line);padding:24px 16px 22px;
       display:flex;flex-direction:column;gap:22px;position:sticky;top:0;align-self:start;height:100vh}
 .sidelogo{font-family:'Cormorant Garamond',Georgia,serif;font-size:23px;line-height:1.12;padding:0 8px}
 .sidelogo span{display:block;font-family:Onest,sans-serif;font-size:9px;letter-spacing:.17em;
                text-transform:uppercase;color:var(--muted);margin-top:5px;font-weight:400}
 .sidenav{display:flex;flex-direction:column;gap:4px}
 .sidenav a{display:flex;align-items:center;gap:11px;padding:9px 12px;border-radius:11px;
            color:#4e473f;text-decoration:none;font-size:14px;transition:background .12s,color .12s;
            min-width:0}
 .sidenav a svg{flex:0 0 auto;width:17px;height:17px;stroke:currentColor;fill:none;
                stroke-width:1.4;stroke-linecap:round;stroke-linejoin:round;opacity:.75}
 .sidenav a:hover{background:rgba(255,255,255,.65)}
 .sidenav a.on{background:var(--wine);color:#fff} .sidenav a.on svg{opacity:1}
 .sidetariff{margin-top:auto;background:#fff;border:1px solid var(--line);border-radius:16px;padding:15px 16px}
 .st-k{font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted)}
 .st-n{font-family:'Cormorant Garamond',Georgia,serif;font-size:19px;line-height:1.15;color:var(--wine);margin:5px 0 4px}
 .st-d{font-size:12px;color:var(--muted);line-height:1.4} .st-d b{color:#4e473f;font-weight:500}
 .sidetariff a{display:block;margin-top:12px;text-align:center;padding:9px;border:1px solid var(--line);
               border-radius:10px;color:var(--wine);text-decoration:none;font-size:12.5px}
 .sidetariff a:hover{border-color:var(--wine)}

 /* ── рабочая область ─────────────────────────────────────────────────────────────────── */
 /* max-width: на мониторе 2560px дашборд растягивался во всю ширину — строки становились
    нечитаемо длинными, карточки неоправданно огромными. Ограничиваем и центруем. */
 .main{padding:22px 26px 60px;min-width:0;max-width:1560px;margin:0 auto;width:100%}
 .panel{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px 20px;min-width:0}
 .ph{font-family:'Cormorant Garamond',Georgia,serif;font-size:22px;line-height:1.15;margin:0;
     display:flex;align-items:center;gap:9px;flex-wrap:wrap}
 .ph .tag{font-family:Onest,sans-serif;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
          color:var(--violet);background:#F1EEF8;border:1px solid #ddd6ee;border-radius:999px;padding:3px 10px}
 .ph .more{margin-left:auto;font-family:Onest,sans-serif;font-size:12px;color:var(--wine);
           text-decoration:none;font-weight:400;white-space:nowrap}
 .psub{font-size:12px;color:var(--muted);margin:4px 0 0;line-height:1.45}
 .secttl{font-family:'Cormorant Garamond',Georgia,serif;font-size:25px;margin:26px 0 4px;line-height:1.1}
 .hint{color:var(--muted);font-size:13px;margin:2px 0 12px}

 /* шапка профиля */
 .profbar{display:flex;align-items:center;gap:15px;background:#fff;border:1px solid var(--line);
          border-radius:18px;padding:14px 18px;margin-bottom:16px;flex-wrap:wrap}
 .profav{width:54px;height:54px;border-radius:50%;background:var(--wine);color:#fff;display:flex;
         align-items:center;justify-content:center;font-family:'Cormorant Garamond',serif;
         font-size:24px;flex:0 0 auto}
 .profname{font-family:'Cormorant Garamond',Georgia,serif;font-size:26px;line-height:1.1}
 .proff{font-size:14px;color:var(--muted)}
 .proff b{color:var(--wine2);font-weight:400;font-family:'Cormorant Garamond',Georgia,serif;font-size:16px}
 .profchips{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
 .profchip{display:flex;align-items:center;gap:9px;background:var(--soft);border:1px solid var(--line);
           border-radius:13px;padding:7px 15px}
 .profchip .pi{font-size:15px;line-height:1}
 .profchip > span:last-child{min-width:0}
 .profchip .pk{font-size:11px;color:var(--muted);display:block}
 .profchip .pv{font-size:13.5px;color:var(--ink);display:block;
               overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .profedit{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:11px;
           padding:10px 15px;font-size:13px;color:#4e473f;text-decoration:none;background:#fff}
 .profedit:hover{border-color:var(--wine);color:var(--wine)}

 /* ── ряд 1: конструктор капсулы + образ на сегодня ───────────────────────────────────── */
 .row2{display:grid;grid-template-columns:minmax(0,1.06fr) minmax(0,.94fr);gap:16px;align-items:start}
 .seasons{display:flex;flex-wrap:wrap;gap:7px;margin:12px 0 0}
 .seasons a{padding:6px 13px;border:1px solid var(--line);border-radius:999px;font-size:12.5px;
            color:var(--ink);text-decoration:none;background:var(--soft)}
 .seasons a.on{background:var(--wine);color:#fff;border-color:var(--wine)}
 .seasons a.notbuilt:not(.on){color:var(--muted);border-style:dashed}
 .capdiff{margin:14px 0 4px;padding:13px 15px;border:1px solid rgba(93,34,48,.14);border-radius:13px;
         background:linear-gradient(135deg,#fbf6ec,#fff)}
.capdiffhead b{display:block;font-size:13.5px;color:var(--ink)}
.capdiffhead span{display:block;font-size:11.5px;color:var(--muted);margin-top:2px}
.capdiffcols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 22px;margin-top:11px}
.capdifflab{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.capdiffrow{display:flex;gap:8px;align-items:flex-start;margin:5px 0;font-size:12px;line-height:1.4}
.capdiffrow i{flex:0 0 16px;height:16px;border-radius:50%;display:flex;align-items:center;justify-content:center;
              font-style:normal;font-size:11px;line-height:1}
.capdiffrow i.out{background:#efe4e4;color:#a5453a}
.capdiffrow i.in{background:#e6efe8;color:#3f7d54}
.capdiffrow b{font-weight:500}
.itemtoggle{display:flex;gap:7px;margin:12px 0 0}
 .itemtoggle a{font-size:12.5px;padding:6px 13px;border:1px solid var(--line);border-radius:999px;
               text-decoration:none;color:var(--ink);background:var(--soft)}
 .itemtoggle a.on{background:var(--wine);color:#fff;border-color:var(--wine)}
 /* minmax(0,…), а не 1fr: у подписи вещи nowrap, и в обычном 1fr она задавала колонке
    min-content — колонки разъезжались по ширине, а плитки прыгали по высоте. */
 .slotgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(104px,1fr));gap:10px;margin-top:14px}
 /* min-width:0 обязателен: у подписи вещи nowrap, и без него автоминимум grid-элемента равен
    ширине всей строки («Верхний слой · вариант 1») — плитки вылезали за экран телефона и
    страница ехала вбок. */
 .pitem{cursor:grab;border:1px solid var(--line);border-radius:11px;background:#fff;padding:5px;
        text-align:center;user-select:none;transition:border-color .12s,box-shadow .12s;
        display:flex;flex-direction:column;min-width:0;max-width:100%;height:100%}
 .pitem:hover{border-color:var(--wine)}
 .pitem.on{border-color:var(--wine);box-shadow:0 0 0 2px rgba(93,34,48,.28)}
 .pitem img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:7px;display:block;background:var(--sand)}
 .pitem .ph0{width:100%;aspect-ratio:3/4;border-radius:7px;background:var(--sand);display:block}
 .pitem .pname{display:-webkit-box;font-size:10.5px;color:#4a443c;margin-top:6px;line-height:1.25;
               -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;max-width:100%;min-height:2.6em}
 .checks{margin-top:14px;display:flex;flex-direction:column;gap:6px}
 .checks div{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
 .checks i{flex:0 0 auto;width:14px;height:14px;border-radius:4px;background:var(--wine);color:#fff;
           font-style:normal;font-size:9px;display:flex;align-items:center;justify-content:center}

 /* образ на сегодня */
 .todaygrid{display:grid;grid-template-columns:1fr;gap:14px;margin-top:14px;align-items:start}
 /* Ячейки идут группами «Основа образа» / «Завершение»: человек собирает образ снизу вверх
    по логике одевания, а не по алфавиту слотов. Внутри группы — сетка в две колонки. */
 .cells{display:flex;flex-direction:column;gap:12px}
 .cellgroup{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
 .cellgrouplab{grid-column:1/-1;font-size:10px;letter-spacing:.16em;text-transform:uppercase;
               color:var(--muted);margin-bottom:-2px}
 .cell{border:1.4px dashed #cdbfa6;border-radius:11px;padding:9px 11px;background:var(--soft);
       min-height:76px;display:flex;flex-direction:column;gap:5px;transition:border-color .12s,background .12s}
 .cell.filled{border-style:solid;border-color:var(--wine);background:#fff}
 .cell.drop{border-color:var(--wine);background:#fdeee2}
 .cellslot{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
 .cellbody{display:grid;grid-template-columns:30px minmax(0,1fr) auto;align-items:center;gap:8px;min-height:40px}
 .cellbody .thumb{width:30px;aspect-ratio:3/4;object-fit:cover;border-radius:5px;background:var(--sand);flex:0 0 auto}
 .cellval{font-size:11.5px;color:var(--ink);line-height:1.3;
          display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-width:0}
 .cell.filled .cellval{font-weight:500}
 .cellbody .buy{font-size:10.5px;color:var(--wine);text-decoration:none;white-space:nowrap;align-self:start}
 .wbox{border:1px solid var(--line);border-radius:14px;padding:14px 15px;background:var(--soft)}
 .wtemp{display:flex;align-items:baseline;gap:9px;margin-top:6px}
 .wtemp b{font-family:'Cormorant Garamond',serif;font-size:32px;color:var(--wine);line-height:1}
 .wk{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
 .wd0{font-size:12px;color:var(--muted);margin-top:4px;line-height:1.4}
 .wrow2{margin-top:12px;padding-top:11px;border-top:1px solid var(--line);font-size:12.5px;color:#4e473f}
 .wrow2 b{color:var(--wine);font-weight:500}
 .chipwhy{margin-top:12px;padding-top:11px;border-top:1px solid rgba(93,34,48,.10)}
 .chipwhylab{font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:7px}
 .chipwhyrow{display:flex;gap:8px;align-items:baseline;margin:5px 0;font-size:12px;line-height:1.4}
 .chipwhyrow b{flex:0 0 auto;font-weight:500;color:var(--wine)}
 .chipwhyrow span{color:var(--muted)}
 .cityform{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:7px;margin-top:11px}
 .cityform input{flex:1;min-width:0;padding:8px 11px;border:1px solid var(--line);border-radius:9px;
                 font:inherit;font-size:12.5px;background:#fff}
 .cityform button{padding:8px 13px;border:0;border-radius:9px;background:var(--wine);color:#fff;
                  font:inherit;font-size:12px;cursor:pointer}
 .ctrls{display:flex;gap:9px;align-items:center;margin-top:12px;flex-wrap:wrap}
 .ctrls button{font:inherit;font-size:12.5px;padding:8px 14px;border-radius:9px;cursor:pointer;
               border:1px solid var(--line);background:#fff;color:var(--wine)}
 .ctrls .cnt{color:var(--muted);font-size:12px}
 .weekdays{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}
 .wd{position:relative;width:36px;height:32px;border:1px solid var(--line);border-radius:9px;
     background:var(--soft);color:var(--ink);font:inherit;font-size:12.5px;cursor:pointer;transition:all .12s}
 .wd:hover{border-color:var(--wine)}
 .wd.on{background:var(--wine);color:#fff;border-color:var(--wine)}
 .wd.filled::after{content:'';position:absolute;top:4px;right:5px;width:5px;height:5px;border-radius:50%;background:var(--wine)}
 .wd.on.filled::after{background:#fff}
 .btnviolet{display:inline-flex;align-items:center;gap:8px;margin-top:13px;background:var(--violet);
            color:#fff;text-decoration:none;padding:11px 20px;border-radius:11px;font-size:12.5px;
            letter-spacing:.06em;text-transform:uppercase}

 /* план недели: лента из семи дней, сегодня выделен */
 .weekgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin-top:13px}
 .weekday{border:1px solid var(--line);border-radius:12px;padding:9px;background:var(--soft);
          display:flex;flex-direction:column;min-width:0;min-height:100%}
 .weekday.on{border-color:var(--wine);background:#fff;box-shadow:0 0 0 1px rgba(93,34,48,.18)}
 .wdname{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-bottom:7px}
 .weekday.on .wdname{color:var(--wine)}
 .wdimg{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:8px;background:var(--sand);display:block}
 .wdimg.empty{background:var(--sand)}
 .wdrole{font-size:11.5px;line-height:1.3;margin-top:7px;font-weight:500;
         display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.7em}
 .wdtags{font-size:10px;color:var(--muted);line-height:1.3;margin-top:4px;
         display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
 /* ── ряд 2: AI-помощник + докупки ────────────────────────────────────────────────────── */
 .row2b{display:grid;grid-template-columns:minmax(280px,.8fr) minmax(0,1.2fr);gap:16px;margin-top:16px;align-items:start}
 .helper{display:flex;flex-direction:column;gap:2px;margin-top:12px}
 .hrow{display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid var(--line);
       text-decoration:none;color:inherit}
 .hrow:last-child{border-bottom:0}
 .hico{flex:0 0 auto;width:30px;height:30px;border-radius:9px;background:#F1EEF8;color:var(--violet);
       display:flex;align-items:center;justify-content:center;font-size:14px}
 .hrow b{display:block;font-size:13.5px;font-weight:500}
 .hrow span{display:block;font-size:11.5px;color:var(--muted);line-height:1.4;margin-top:2px}
 .buygrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:11px;margin-top:13px}
 .buycard{border:1px solid var(--line);border-radius:13px;padding:12px 13px;background:var(--soft);
          display:flex;flex-direction:column;min-width:0}
 .buyname{font-size:13px;font-weight:500;line-height:1.3;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
 .buywhy{font-size:11.5px;color:var(--muted);line-height:1.4;margin:7px 0 0;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
 .buyok{margin-top:auto;padding-top:9px;font-size:11px;color:#3a5a3a;display:flex;align-items:center;gap:6px}
 .buyok i{font-style:normal;color:#3a7a4a}
 .buyfoot{margin-top:12px;font-size:12px;color:var(--wine)}
 .buyfoot a{color:var(--wine);text-decoration:none}

 /* ── ряд 3: четыре плитки возможностей ───────────────────────────────────────────────── */
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-top:16px}
 .tile{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 17px;
       display:flex;flex-direction:column}
 .tile .ti{width:30px;height:30px;border-radius:9px;background:#F1EEF8;color:var(--violet);
           display:flex;align-items:center;justify-content:center;font-size:14px;margin-bottom:10px}
 .tile b{font-size:14px;font-weight:500}
 .tile p{font-size:12px;color:var(--muted);line-height:1.45;margin:5px 0 12px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
 .tile a{margin-top:auto;font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;
         color:var(--violet);text-decoration:none}

 /* ── лента «кабинет продолжает Карту» ────────────────────────────────────────────────── */
 .footband{display:flex;align-items:center;gap:18px;background:linear-gradient(135deg,#fff,#f6f3fb);
           border:1px solid #e2dcef;border-radius:18px;padding:20px 24px;margin-top:18px;flex-wrap:wrap}
 .footband .mono{flex:0 0 auto;width:44px;height:44px;border-radius:12px;background:#F1EEF8;
                 color:var(--violet);display:flex;align-items:center;justify-content:center;font-size:19px}
 .foottext b{display:block;font-size:14px;font-weight:500}
 .foottext span{display:block;font-size:12.5px;color:var(--muted);line-height:1.5;margin-top:3px;max-width:520px}
 .footbtn{margin-left:auto;background:var(--violet);color:#fff;text-decoration:none;padding:13px 26px;
          border-radius:11px;font-size:12.5px;letter-spacing:.07em;text-transform:uppercase}

 /* ── секции ниже панели ──────────────────────────────────────────────────────────────── */
 .trk{background:#fff;border:1px solid var(--line);border-radius:18px;padding:18px 20px;margin-top:10px}
 .trow{display:flex;align-items:center;gap:12px;margin:10px 0;font-size:12.5px}
 .tdate{flex:0 0 128px;color:var(--muted)} .tdate b{color:var(--wine);font-weight:normal}
 .tbar{flex:1;height:10px;background:#efe8db;border-radius:999px;overflow:hidden}
 .tfill{display:block;height:100%;background:linear-gradient(90deg,var(--wine),#8a3346);border-radius:999px;
        width:0;transition:width 1s cubic-bezier(.22,1,.36,1)}
 .tval{flex:0 0 40px;text-align:right;font-variant-numeric:tabular-nums;color:var(--wine)}
 .tnote{font-size:12.5px;color:var(--muted);margin:10px 0 0;line-height:1.5}
 .miles{display:flex;flex-wrap:wrap;gap:9px;margin:10px 0 4px}
 .mile{flex:1;min-width:110px;background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:11px 13px}
 .mile .mn{font-family:'Cormorant Garamond',serif;font-size:24px;line-height:1;color:var(--ink);
           font-variant-numeric:tabular-nums}
 .mile .mn.win{color:#3a5a3a}
 .mile .ml{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-top:5px}
 .roles3{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}
 .role3{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden;display:flex;flex-direction:column}
 .roleimg{aspect-ratio:4/5;overflow:hidden;background:var(--sand)}
 .roleimg img{width:100%;height:100%;object-fit:cover;display:block}
 .rolebody{padding:13px 15px}
 .role3 .rb{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine)}
 .role3 .rn{font-family:'Cormorant Garamond',serif;font-size:18px;line-height:1.15;margin:5px 0 8px}
 .role3 ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
 .role3 li{font-size:12px;color:var(--muted);line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
 .role3 li::before{content:"— ";color:var(--wine)}
 .minegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
 .mineitem{background:#fff;border:1px solid var(--line);border-radius:14px;padding:13px 15px;position:relative}
 .mineslot{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--wine)}
 .minename{font-size:14px;margin:5px 0 4px;line-height:1.4}
 .mineverdict{font-size:11.5px;color:var(--muted)}
 .mineremove{position:absolute;top:10px;right:12px;margin:0}
 .mineremove button{border:0;background:none;color:var(--muted);font:inherit;font-size:11.5px;
                    cursor:pointer;text-decoration:underline}
 .nudge{display:flex;gap:12px;align-items:center;background:#fbf3e8;border:1px solid #e8d9c2;
        border-radius:14px;padding:12px 16px;margin-top:14px;font-size:13px;color:#7a5b32;flex-wrap:wrap}
 .nudge a{margin-left:auto;white-space:nowrap;background:var(--wine);color:#fff;text-decoration:none;
          padding:8px 14px;border-radius:9px;font-size:12.5px}
 .empty{color:var(--muted);font-size:13px;background:#fff;border:1px solid var(--line);
        border-radius:12px;padding:16px}
 .fb{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:16px}
 .fb h2{font-family:'Cormorant Garamond',serif;font-size:23px;margin:0 0 4px}
 .fb p.h{color:var(--muted);font-size:13.5px;margin:0 0 14px}
 .stars{display:flex;gap:6px;margin-bottom:12px;flex-direction:row-reverse;justify-content:flex-end}
 .stars input{position:absolute;opacity:0;width:0;height:0}
 .stars label{font-size:25px;color:#d9cfbf;cursor:pointer;line-height:1;transition:color .12s}
 .stars label:hover,.stars label:hover~label,.stars input:checked~label{color:#c8a24a}
 .fb textarea{width:100%;padding:11px 13px;border:1px solid #d9d2c7;border-radius:10px;font:inherit;
              font-size:14.5px;resize:vertical}
 .fb button{margin-top:12px;padding:12px 24px;background:var(--wine);color:#fff;border:0;border-radius:10px;
            font:inherit;font-size:14.5px;cursor:pointer}
 .fb .done{margin:0;font-size:15px}

 @media(max-width:1360px){.row2{grid-template-columns:1fr}.row2b{grid-template-columns:1fr}.slotgrid{grid-template-columns:repeat(auto-fill,minmax(96px,1fr))}}
 @media(max-width:1280px){.tiles{grid-template-columns:1fr 1fr}}
 @media(max-width:1080px){
  .shell{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;flex-wrap:wrap;align-items:center;gap:14px}
  .sidenav{flex-direction:row;flex-wrap:wrap} .sidetariff{margin:0;flex:1 1 240px}
  .todaygrid{grid-template-columns:1fr}
  .profchips{margin-left:0;width:100%}
 }
 @media(max-width:760px){
  .main{padding:18px 16px 50px}
  .buygrid,.roles3,.tiles{grid-template-columns:1fr}
  .cellgroup{grid-template-columns:1fr}
  .cityform{grid-template-columns:1fr}
  .footbtn{margin-left:0;width:100%;text-align:center}
 }
 @media(prefers-reduced-motion:reduce){.tfill{transition:none}}
</style></head><body>
{% set fparts = (formula or '').split('×') %}
<div class=shell>
<div class=side>
 <div class=sidelogo>Чувство стиля<span>твоя формула стиля</span></div>
 <nav class=sidenav>
  <a href="/card"><svg viewBox="0 0 20 20"><rect x="2.5" y="3.5" width="15" height="13" rx="2"/><path d="M2.5 8h15M8 8v8.5"/></svg>Моя карта стиля</a>
  <a class=on href="#top"><svg viewBox="0 0 20 20"><rect x="2.5" y="4" width="15" height="13" rx="2"/><path d="M2.5 8h15M6.5 2.5v3M13.5 2.5v3"/></svg>Стиль каждый день</a>
  <a href="#wardrobe"><svg viewBox="0 0 20 20"><rect x="2.5" y="2.5" width="6" height="6" rx="1.4"/><rect x="11.5" y="2.5" width="6" height="6" rx="1.4"/><rect x="2.5" y="11.5" width="6" height="6" rx="1.4"/><rect x="11.5" y="11.5" width="6" height="6" rx="1.4"/></svg>Конструктор</a>
  <a href="#week"><svg viewBox="0 0 20 20"><rect x="2.5" y="4" width="15" height="13" rx="2"/><path d="M2.5 8h15M6.5 2.5v3M13.5 2.5v3"/></svg>План недели</a>
  <a href="#roles"><svg viewBox="0 0 20 20"><rect x="3" y="2.5" width="14" height="15" rx="2"/><circle cx="10" cy="7.5" r="2.2"/><path d="M5.5 16c1-2.6 2.6-4 4.5-4s3.5 1.4 4.5 4"/></svg>Образы и роли</a>
  <a href="#shopping"><svg viewBox="0 0 20 20"><path d="M4 6.5h12l-1 10.5H5z"/><path d="M7.2 6.5V5a2.8 2.8 0 0 1 5.6 0v1.5"/></svg>Покупки</a>
  {% if track %}<a href="#track"><svg viewBox="0 0 20 20"><path d="M3 17h14"/><rect x="4.5" y="10" width="3" height="5" rx=".8"/><rect x="9" y="6" width="3" height="9" rx=".8"/><rect x="13.5" y="3" width="3" height="12" rx=".8"/></svg>Прогресс</a>{% endif %}
  {% if mine %}<a href="#wardrobe-mine"><svg viewBox="0 0 20 20"><path d="M10 16.5S3.5 12.6 3.5 8.2A3.6 3.6 0 0 1 10 6a3.6 3.6 0 0 1 6.5 2.2c0 4.4-6.5 8.3-6.5 8.3z"/></svg>Мои вещи</a>{% endif %}
 </nav>
 <div class=sidetariff>
  <div class=st-k>Тариф</div>
  <div class=st-n>Стиль каждый день</div>
  <div class=st-d>Ежедневная поддержка и умный гардероб{% if season_label %}<br><b>{{ season_label }}</b>{% endif %}</div>
  <a href="/card">Вернуться в Карту</a>
 </div>
</div>

<div class=main id=top>

<div class=profbar>
 <div class=profav>{{ (email or 'К')[0]|upper }}</div>
 <div>
  <div class=profname>Стиль каждый день</div>
  <div class=proff><b>{{ formula }}</b></div>
 </div>
 <div class=profchips>
  {% if season_label %}<div class=profchip><span class=pi>❦</span><span><span class=pk>Сезон</span><span class=pv>{{ season_label }}</span></span></div>{% endif %}
  {% if colortype %}<div class=profchip><span class=pi>✦</span><span><span class=pk>Цветотип</span><span class=pv>{{ colortype }}</span></span></div>{% endif %}
  {# Индекс = 100 − разрыв, ровно как в Карте. Здесь годами показывался сырой gap: Карта говорила
     «индекс 69%», кабинет на соседнем экране — «Индекс 31%». Одна метрика, два обратных числа. #}
  {% if gap_now is not none %}<div class=profchip><span class=pi>◔</span><span><span class=pk>Индекс</span><span class=pv>{{ 100 - gap_now }}%</span></span></div>{% endif %}
  <a class=profedit href="/me">Профиль <span>✎</span></a>
 </div>
</div>

{# ── ряд 1: конструктор капсулы · образ на сегодня ───────────────────────────────────── #}
<div class=row2>

 <div class=panel id=wardrobe>
  <h2 class=ph>Капсульный конструктор образов<span class=tag>живой гардероб</span></h2>
  <p class=psub>Собери образ из своей капсулы: нажми или перетащи вещь в ячейку справа. Это вещи из базы брендов — каждую можно добавить в капсулу.</p>
  {% if season_tabs %}
  <div class=seasons>
   {% for s in season_tabs %}<a href="/cabinet?season={{ s.code }}" class="{{ 'on' if s.on else '' }}{{ ' notbuilt' if not s.built else '' }}" title="{{ 'капсула собрана' if s.built else 'подберётся из каталога' }}">{{ s.label }}</a>{% endfor %}
  </div>
  {% endif %}
  {# Эволюция капсулы: капсула не просто «другая» в новом сезоне — видно, что ушло, что пришло
     и что это дало по сочетаниям. Без этого блока переключение сезона выглядело случайным. #}
  {% if season_diff %}
  <div class=capdiff>
   <div class=capdiffhead><b>Капсула пересобрана: {{ season_diff.from_label }} → {{ season_diff.to_label }}</b>
    <span>{{ season_diff.kept_count }} вещей остались · сочетаний {{ season_diff.combinations_before }} → {{ season_diff.combinations_after }}</span></div>
   <div class=capdiffcols>
    {% if season_diff.removed %}
    <div class=capdiffcol>
     <div class=capdifflab>Ушли из капсулы</div>
     {% for r in season_diff.removed[:4] %}<div class=capdiffrow><i class=out>−</i><span><b>{{ r.name }}</b> — {{ r.why }}</span></div>{% endfor %}
    </div>
    {% endif %}
    {% if season_diff.added %}
    <div class=capdiffcol>
     <div class=capdifflab>Пришли на замену</div>
     {% for a in season_diff.added[:4] %}<div class=capdiffrow><i class=in>+</i><span><b>{{ a.name }}</b>{% if a.adds_looks %} — плюс {{ a.adds_looks }} {{ 'образ' if a.adds_looks == 1 else 'образа' if a.adds_looks < 5 else 'образов' }}{% endif %}</span></div>{% endfor %}
    </div>
    {% endif %}
   </div>
  </div>
  {% endif %}
  <div class=itemtoggle>
   <a href="/cabinet?items=6{% if sel_season %}&season={{ sel_season }}{% endif %}" class="{{ 'on' if items_n == 6 else '' }}">6 опорных вещей</a>
   <a href="/cabinet?items=12{% if sel_season %}&season={{ sel_season }}{% endif %}" class="{{ 'on' if items_n == 12 else '' }}">Расширенная 12</a>
  </div>
  {% if board %}
  <div class=slotgrid>
   {% for grp in board %}{% for it in grp['items'] %}
   <span class=pitem data-slot="{{ grp.slot }}" data-name="{{ it.name }}" data-img="{{ it.image or '' }}" data-url="{{ it.url or '' }}">
    {% if it.image %}<img src="{{ it.image }}" alt="" loading=lazy>{% else %}<span class=ph0></span>{% endif %}
    <span class=pname title="{{ it.name }}">{{ it.name }}</span>
   </span>
   {% endfor %}{% endfor %}
  </div>
  <div class=checks>
   <div><i>✓</i>Перетаскивай вещи между ячейками</div>
   <div><i>✓</i>Собирай образ на каждый день недели</div>
   <div><i>✓</i>Вещи подобраны под твою Формулу и палитру</div>
  </div>
  {% else %}
  <p class=empty style="margin-top:14px">Капсула ещё не собрана. <a href="/card">Собери Карту стиля</a> — вещи появятся здесь.</p>
  {% endif %}
 </div>

 <div class=panel>
  <h2 class=ph>Твой образ на сегодня</h2>
  {% if weekview %}<p class=psub>{{ weekview['today']['body'] }}</p>{% endif %}
  <div class=todaygrid>
   <div>
    <div class=cells>
     {% for g in outfit_cells %}
     <div class=cellgroup>
      <div class=cellgrouplab>{{ g.title }}</div>
      {% for slot in g.slots %}
      <div class=cell data-cell="{{ slot }}"><span class=cellslot>{{ slot }}</span><span class=cellbody><span class=cellval>—</span></span></div>
      {% endfor %}
     </div>
     {% endfor %}
     {% if not outfit_cells %}<p class=empty>Ячейки появятся вместе с капсулой.</p>{% endif %}
    </div>
    <div class=weekdays>
     <button type=button class=wd data-day=mon>Пн</button>
     <button type=button class=wd data-day=tue>Вт</button>
     <button type=button class=wd data-day=wed>Ср</button>
     <button type=button class=wd data-day=thu>Чт</button>
     <button type=button class=wd data-day=fri>Пт</button>
     <button type=button class=wd data-day=sat>Сб</button>
     <button type=button class=wd data-day=sun>Вс</button>
    </div>
    <div class=ctrls><button type=button onclick=clearOutfit()>Очистить образ</button><span class=cnt>вещей: <b id=count>0</b></span></div>
   </div>
   <div>
    <div class=wbox>
     {% if weather %}
     <div class=wk>Погода{% if weather.city %} в городе {{ weather.city }}{% endif %}</div>
     <div class=wtemp><b>{{ weather.temp }}°</b><span class=wd0>{{ weather.description }}</span></div>
     {% if dress %}<div class=wd0>{{ dress.note }}</div>{% endif %}
     {% else %}
     <div class=wk>Погода</div>
     <p class=wd0>Укажи город — и образ на день будет учитывать, что надеть поверх капсулы.</p>
     {% endif %}
     <div class=wrow2>
      {% if weekview %}Роль: <b>{{ weekview['today']['title'] }}</b><br>{% endif %}
      {% if want_traits %}Настроение: <b>{{ want_traits[0] }}</b>{% endif %}
      {% if dress %}<br>Поверх: <b>{{ dress.layer }}</b>{% endif %}
     </div>
     {# Explainable-слой: клиентка видит связь «условие → следствие», а не просто готовый образ.
        Считается кодом (outfit_chips), поэтому при тех же входных данных объяснение то же. #}
     {% if chips %}
     <div class=chipwhy>
      <div class=chipwhylab>Почему образ такой</div>
      {% for c in chips %}<div class=chipwhyrow><b>{{ c.label }}</b><span>{{ c.why }}</span></div>{% endfor %}
     </div>
     {% endif %}
     {% if weather_on %}
     <form method=post action="/cabinet/city" class=cityform>
      <input name=city value="{{ city }}" placeholder="Город" aria-label="Город">
      <button type=submit>Обновить</button>
     </form>
     {% endif %}
    </div>
    <a class=btnviolet href="#week">Смотреть план недели</a>
   </div>
  </div>
 </div>

</div>

{# ── План недели: семь дней с ролью и образом. Механика считается серверно (_daily_week_view),
   при перевёрстке её показ был потерян — плитка «План недели» вела в никуда. #}
{% if weekview and weekview['week'] %}
<div class=panel style="margin-top:16px" id=week>
 <h2 class=ph>План недели<span class=tag>под роли и погоду</span></h2>
 <p class=psub>Семь дней из твоей капсулы. Сегодня выделено — с него и начинай.</p>
 <div class=weekgrid>
  {% for row in weekview['week'] %}
  <div class="weekday{{ ' on' if row['day'] == today_label else '' }}">
   <div class=wdname>{{ row['day'] }}</div>
   {% if row['img'] %}<img class=wdimg src="{{ row['img'] }}" alt="{{ row['title'] }}" loading=lazy>
   {% else %}<span class="wdimg empty"></span>{% endif %}
   <div class=wdrole>{{ row['title'] }}</div>
   {% if row['tags'] %}<div class=wdtags>{{ row['tags']|join(' · ') }}</div>{% endif %}
  </div>
  {% endfor %}
 </div>
</div>
{% endif %}

{# ── ряд 2: AI-помощник · что докупить ───────────────────────────────────────────────── #}
<div class=row2b>

 <div class=panel>
  <h2 class=ph>Навигатор гардероба</h2>
  <div class=helper>
   <a class=hrow href="/garment"><span class=hico>✓</span><span><b>Брать или не брать</b><span>Проверить вещь перед покупкой</span></span></a>
   <a class=hrow href="#week"><span class=hico>▦</span><span><b>План недели</b><span>Готовые образы на неделю под роли, погоду и твоё время</span></span></a>
   <a class=hrow href="/wardrobe"><span class=hico>◫</span><span><b>Мой гардероб</b><span>Загрузи свои вещи — разберём по Формуле и покажем, сколько образов уже есть</span></span></a>
   <a class=hrow href="#wardrobe"><span class=hico>❋</span><span><b>Сезонные обновления</b><span>Переключи сезон — капсула пересоберётся под него</span></span></a>
   <a class=hrow href="#track"><span class=hico>◔</span><span><b>Трекер настройки образа</b><span>Видно, как образ догоняет то, какой ты себя хочешь</span></span></a>
  </div>
 </div>

 <div class=panel id=shopping>
  <h2 class=ph>Что добавить в капсулу<span class=tag>точечные рекомендации</span></h2>
  <p class=psub>Точечные вещи, которые усиливают капсулу под твою Формулу и сезон.</p>
  {% if shopping %}
  <div class=buygrid>
   {% for it in shopping[:3] %}
   <div class=buycard>
    {# В покупках Карты поле называется item_name; шаблон читал it.name и печатал пустоту —
       клиентка видела «почему подходит» без самой вещи. #}
    {% set bn = it.item_name or it.name or '' %}<div class=buyname>{{ bn[0]|upper }}{{ bn[1:] }}</div>
    {% if it.closes_gap %}<p class=buywhy>{{ it.closes_gap }}</p>{% endif %}
    <div class=buyok><i>✓</i>Подходит к твоей капсуле и палитре</div>
   </div>
   {% endfor %}
  </div>
  <div class=buyfoot><a href="/card#shopping">Весь список покупок из Карты →</a></div>
  {% else %}<p class=empty style="margin-top:12px">Лист покупок появится вместе с собранной Картой.</p>{% endif %}
 </div>

</div>

{# ── ряд 3: что умеет кабинет ────────────────────────────────────────────────────────── #}
<div class=tiles>
 <div class=tile><span class=ti>◫</span><b>Капсульный конструктор образов</b><p>Создавай образы из своей капсулы. Все сочетания проверены под твою Формулу.</p><a href="#wardrobe">Открыть конструктор →</a></div>
 <div class=tile><span class=ti>✓</span><b>Брать или не брать</b><p>Проверь любую покупку: подойдёт ли она по стилю, цветотипу и фигуре.</p><a href="/garment">Проверить вещь →</a></div>
 <div class=tile><span class=ti>▦</span><b>План недели</b><p>Семь дней из твоей капсулы: у каждого своя роль и свой образ.</p><a href="#week">Посмотреть план →</a></div>
 <div class=tile><span class=ti>◫</span><b>Мой гардероб</b><p>Разбери свои вещи по Формуле: что работает, что нет и сколько образов уже собирается.</p><a href="/wardrobe">Открыть гардероб →</a></div>
 <div class=tile><span class=ti>❋</span><b>Сезонные обновления</b><p>Капсула пересобирается под сезон: вещи, ткани и слои меняются.</p><a href="#wardrobe">Переключить сезон →</a></div>
</div>

<div class=footband>
 <div class=mono>✦</div>
 <div class=foottext>
  <b>Живой гардероб использует твою Формулу и опорную капсулу из Карты стиля</b>
  <span>Мы не пересобираем стиль заново — ты получаешь ежедневную поддержку, чтобы выглядеть уверенно без лишних трат и стресса.</span>
 </div>
 <a class=footbtn href="/card">Смотреть Карту стиля</a>
</div>

{# ── трекер разрыва ──────────────────────────────────────────────────────────────────── #}
{% if track %}
<h2 class=secttl id=track>Как меняется индекс настройки образа</h2>
<p class=hint>Измеримая трансформация: цифра двигается только от реального пере-замера.</p>
<div class=trk>
 {% if milestones %}
 <div class=miles>
  <div class=mile><div class=mn>{{ milestones.start }}%</div><div class=ml>старт</div></div>
  <div class=mile><div class=mn>{{ milestones.now }}%</div><div class=ml>сейчас</div></div>
  {% if milestones.delta > 0 %}<div class=mile><div class="mn win">−{{ milestones.delta }}</div><div class=ml>п.п. закрыто</div></div>{% endif %}
  <div class=mile><div class=mn>{{ milestones.count }}</div><div class=ml>{% if milestones.count > 1 %}замера{% else %}замер{% endif %}</div></div>
 </div>
 {% endif %}
 {% for p in track.points %}
 <div class=trow>
  <span class=tdate>{{ p.date }}{% if loop.first %} · <b>старт</b>{% endif %}</span>
  <span class=tbar><span class=tfill data-w="{{ p.gap }}"></span></span>
  <span class=tval>{{ p.gap }}%</span>
 </div>
 {% endfor %}
 {% if track.measurements < 2 %}
 <p class=tnote>Это точка отсчёта. Сделай пере-замер через время — увидишь, как меняется индекс настройки образа.</p>
 {% else %}
 <p class=tnote>Индекс настройки образа меняется, и это видно. Двигается он только от настоящего пере-замера, поэтому цифре можно верить.</p>
 {% endif %}
</div>
{% endif %}

{% if days_since is not none and days_since >= 30 %}
<div class=nudge><span>С последнего замера прошло {{ days_since }} дней. Пере-замер покажет, как изменился индекс настройки образа за это время.</span><a href="/identity-scan-quiz.html?fresh=1">Сделать пере-замер</a></div>
{% endif %}

{# ── роли недели ─────────────────────────────────────────────────────────────────────── #}
{% if roles %}
<h2 class=secttl id=roles>Роли твоей недели</h2>
<p class=hint>Одна капсула — разные роли твоего дня. Так формула работает под каждую жизненную ситуацию.</p>
<div class=roles3>
 {% for r in roles %}
 <div class=role3>
  {% if r.img %}<div class=roleimg><img src="{{ r.img }}" alt="Образ · {{ r.bucket }}" loading=lazy></div>{% endif %}
  <div class=rolebody>
   <div class=rb>{{ r.bucket }}</div>
   <div class=rn>{% if r.name %}{{ r.name }}{% else %}{{ r.scenario }}{% endif %}</div>
   {% if r.pieces %}<ul>{% for it in r.pieces %}<li>{{ it }}</li>{% endfor %}</ul>{% endif %}
  </div>
 </div>
 {% endfor %}
</div>
{% endif %}

{# ── вещи, которые клиентка забрала после проверки «брать / не брать» ─────────────────── #}
{% if mine %}
<h2 class=secttl id=wardrobe-mine>Твои вещи</h2>
<p class=hint>Вещи, которые ты проверила и решила взять. Они учитываются, когда собираешь образ.</p>
<div class=minegrid>
 {% for it in mine %}
 <div class=mineitem>
  <div class=mineslot>{{ it.slot or 'Вещь' }}</div>
  <div class=minename>{{ it.name }}</div>
  {% if it.verdict %}<div class=mineverdict>{{ it.verdict }}</div>{% endif %}
  <form method=post action="/wardrobe/remove" class=mineremove>
   <input type=hidden name=id value="{{ it.id }}">
   <button type=submit title="Убрать из гардероба">убрать</button>
  </form>
 </div>
 {% endfor %}
</div>
{% endif %}

<div class=fb id=review>
{% if thanks %}
 <p class=done>Спасибо. Твой отзыв записан — он помогает нам делать сервис точнее.</p>
{% else %}
 <h2>Как тебе твой гардероб?</h2>
 <p class=h>Оцени и напиши пару слов — что откликнулось, чего не хватило.</p>
 <form method=post action="/card/feedback">
  <input type=hidden name=next value="/cabinet{% if sel_season %}?season={{ sel_season }}{% endif %}">
  <div class=stars>
   {% for n in [5,4,3,2,1] %}<input type=radio name=rating id="st{{ n }}" value="{{ n }}"><label for="st{{ n }}" title="{{ n }}">★</label>{% endfor %}
  </div>
  <textarea name=text rows=3 placeholder="Что откликнулось, чего не хватило?"></textarea>
  <button type=submit>Отправить отзыв</button>
 </form>
{% endif %}
</div>

</div></div>
<script>
// Планировщик недели: образ хранится ПО ДНЯМ в браузере (localStorage), без сервера.
// outfit — всегда образ ТЕКУЩЕГО дня; переключение дня меняет, какой образ показан/редактируется.
var byKey={}, week={}, curDay='mon';
try { week = JSON.parse(localStorage.getItem('senseWeek') || '{}') || {}; } catch(e) { week={}; }
function saveWeek(){ try { localStorage.setItem('senseWeek', JSON.stringify(week)); } catch(e){} }
function dayOutfit(){ if(!week[curDay]) week[curDay]={}; return week[curDay]; }
var outfit;
document.querySelectorAll('.pitem').forEach(function(i){
 byKey[i.getAttribute('data-slot')+'|'+i.getAttribute('data-name')]={
  slot:i.getAttribute('data-slot'), name:i.getAttribute('data-name'),
  img:i.getAttribute('data-img'), url:i.getAttribute('data-url')};
});
function markDays(){
 document.querySelectorAll('.wd').forEach(function(b){
  var d=b.getAttribute('data-day');
  b.classList.toggle('on', d===curDay);
  b.classList.toggle('filled', week[d] && Object.keys(week[d]).length>0);
 });
}
function setDay(d){ curDay=d; outfit=dayOutfit(); markDays(); render(); }
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function cellHtml(o){
 var h='';
 if(o.img) h+='<img class=thumb src="'+esc(o.img)+'" alt="">';
 h+='<span class=cellval>'+esc(o.name)+'</span>';
 if(o.url) h+='<a class=buy href="'+esc(o.url)+'" target="_blank" rel="noopener">купить →</a>';
 return h;
}
function render(){
 document.querySelectorAll('[data-cell]').forEach(function(c){
  var s=c.getAttribute('data-cell'); var o=outfit[s]; var body=c.querySelector('.cellbody');
  if(o){ body.innerHTML=cellHtml(o); c.classList.add('filled'); }
  else { body.innerHTML='<span class=cellval>—</span>'; c.classList.remove('filled'); }
 });
 document.querySelectorAll('.pitem').forEach(function(i){
  var s=i.getAttribute('data-slot');
  i.classList.toggle('on', outfit[s] && outfit[s].name===i.getAttribute('data-name'));
 });
 var cnt=document.getElementById('count'); if(cnt) cnt.textContent=Object.keys(outfit).length;
}
function pickKey(key){ var o=byKey[key]; if(!o) return;
 if(outfit[o.slot] && outfit[o.slot].name===o.name){ delete outfit[o.slot]; } else { outfit[o.slot]=o; }
 saveWeek(); markDays(); render(); }
function clearOutfit(){ week[curDay]={}; outfit=week[curDay]; saveWeek(); markDays(); render(); }
document.querySelectorAll('.pitem').forEach(function(i){
 var key=i.getAttribute('data-slot')+'|'+i.getAttribute('data-name');
 i.setAttribute('draggable','true');
 i.addEventListener('click',function(){ pickKey(key); });
 i.addEventListener('dragstart',function(e){ e.dataTransfer.setData('text', key); });
});
document.querySelectorAll('.wd').forEach(function(b){
 b.addEventListener('click',function(){ setDay(b.getAttribute('data-day')); });
});
setDay(curDay);
document.querySelectorAll('[data-cell]').forEach(function(c){
 c.addEventListener('dragover',function(e){ e.preventDefault(); c.classList.add('drop'); });
 c.addEventListener('dragleave',function(){ c.classList.remove('drop'); });
 c.addEventListener('drop',function(e){ e.preventDefault(); c.classList.remove('drop');
  var key=e.dataTransfer.getData('text')||''; if(key.split('|')[0]===c.getAttribute('data-cell')) pickKey(key); });
});
// Трекер: полосы анимируем от нуля к значению. Значения уже в разметке — корректны без JS.
(function(){
 var fills=document.querySelectorAll('.tfill');
 fills.forEach(function(b){ b.style.width='0%'; });
 requestAnimationFrame(function(){ requestAnimationFrame(function(){
  fills.forEach(function(b){ b.style.width=(b.getAttribute('data-w')||0)+'%'; });
 }); });
})();
</script>
</body></html>"""


def _look_of_the_day(card: dict, weather: dict | None = None) -> dict | None:
    """«Образ дня» с фото — из уже сгенерированных образов Карты, без новой генерации.

    Образы на клиентке стоят денег и минуты времени, поэтому не рендерим заново: берём готовые
    из Карты и раскладываем по дням недели. Будни тянутся к деловым сценариям, выходные — к
    свободным. В мороз и дождь открытые/лёгкие сценарии («свидание», «выход») уступают закрытым.
    """
    looks = [lk for lk in (card.get("looks") or []) if lk.get("img")]
    if not looks:
        return None
    from datetime import date
    wd = date.today().weekday()  # 0 — понедельник
    workday = wd <= 4
    prefer = (["деловая встреча", "презентация", "корпоратив"] if workday
              else ["выходные", "путешествие", "свидание"])
    cold = bool(weather) and weather.get("feels_like", 99) < 5
    if cold:  # в мороз «свидание» с открытым платьем — плохой совет дня
        prefer = [p for p in prefer if p != "свидание"] or prefer

    def _rank(lk):
        scn = (lk.get("scenario") or "").lower()
        for i, p in enumerate(prefer):
            if p in scn:
                return i
        return len(prefer)

    # Ротация ТОЛЬКО среди одинаково подходящих: если брать по индексу дня из общего списка,
    # приоритет ломается — в мороз выпадало «свидание», которое мы только что исключили.
    best = min(_rank(lk) for lk in looks)
    fit = [lk for lk in looks if _rank(lk) == best]
    chosen = fit[wd % len(fit)]
    return {"img": chosen.get("img"), "scenario": chosen.get("scenario") or "Образ дня",
            "desc": chosen.get("desc") or chosen.get("description") or "",
            "why": chosen.get("why") or ""}


def _daily_cabinet_advice(card: dict, diag: dict, track: dict | None,
                          board: list[dict], shopping: list[dict]) -> dict | None:
    """Короткий совет недели для живого кабинета.

    Это не новая диагностика и не новая формула, а практический next step поверх уже собранной
    Карты: что именно сделать на этой неделе, чтобы формула жила в обычной жизни.
    """
    formula = card.get("formula") or diag.get("style_formula")
    if not formula:
        return None
    gap_now = ((track or {}).get("points") or [{}])[-1].get("gap", card.get("gap"))
    delta = (track or {}).get("delta")
    first_role = next((lk.get("scenario") for lk in (card.get("looks") or []) if lk.get("scenario")), None)
    first_buy = next((it.get("name") for it in shopping if it.get("name")), None)

    anchors = []
    for grp in board:
        picked = [it.get("name") for it in (grp.get("items") or [])[:1] if it.get("name")]
        if picked:
            anchors.extend(picked)
        if len(anchors) >= 2:
            break
    anchors = anchors[:2]

    if delta and delta > 0:
        title = "Закрепи прогресс в обычной неделе"
        body = (
            f"Разрыв уже сокращается, поэтому сейчас важно не искать новый стиль, а повторять "
            f"рабочую Формулу «{formula}» в привычных сценариях. Собери 2-3 спокойных образа "
            f"из своей капсулы и проверь новые покупки только через фильтр совместимости."
        )
    elif gap_now is not None and gap_now >= 55:
        title = "Неделя на базу, а не на случайные покупки"
        body = (
            f"Сейчас лучший ход — опереться на уже собранную Формулу «{formula}» и довести до "
            f"автоматизма базовые сочетания. Не расширяй гардероб хаотично: сначала носи капсулу "
            f"в реальных днях, потом докупай только то, чего ей действительно не хватает."
        )
    else:
        title = "Переводи Формулу в повседневные решения"
        body = (
            f"Формула уже собрана, и задача недели — сделать её привычкой. Начинай день с капсулы, "
            f"а не с случайного выбора: так стиль становится повторяемым и начинает экономить время."
        )

    chips = []
    if first_role:
        chips.append(f"фокус недели: {first_role}")
    if anchors:
        chips.append("якорные вещи: " + ", ".join(anchors))
    if first_buy:
        chips.append(f"следующая покупка: {first_buy}")

    return {
        "title": title,
        "body": body,
        "chips": chips,
        "cta_href": "/garment",
        "cta_label": "Проверить вещь перед покупкой",
    }


def _board_week_outfits(board: list[dict]) -> list[dict]:
    """Семь сценариев недели, собранных из текущей активной капсулы.

    Кабинет не должен рассказывать про одну неделю в generated-образах и про другую в конструкторе.
    Поэтому базовый источник правды для недельного плана — текущий `board` капсулы сезона.
    """
    by_slot = {grp.get("slot") or "": [it.get("name") for it in (grp.get("items") or []) if it.get("name")]
               for grp in board or []}

    def pick(slot: str, idx: int = 0) -> str | None:
        items = by_slot.get(slot) or []
        return items[idx] if idx < len(items) else (items[0] if items else None)

    plans = [
        ("Работа", "Деловая встреча", [pick("Верхний слой"), pick("Верх"), pick("Низ"), pick("Обувь")]),
        ("Работа", "Презентация", [pick("Верхний слой"), pick("Верх", 1), pick("Низ"), pick("Обувь", 1)]),
        ("Повседневное", "Выходные", [pick("Верх", 1), pick("Низ", 1), pick("Обувь"), pick("Аксессуары")]),
        ("Выход", "Свидание", [pick("Платья и комбинезоны"), pick("Обувь", 1), pick("Аксессуары"), pick("Верхний слой")]),
        ("Выход", "Корпоратив", [pick("Платья и комбинезоны"), pick("Обувь"), pick("Аксессуары"), pick("Верхний слой")]),
        ("Повседневное", "Путешествие", [pick("Верхний слой"), pick("Верх"), pick("Низ", 1), pick("Обувь")]),
        ("Повседневное", "Повседневное", [pick("Верх"), pick("Низ"), pick("Обувь"), pick("Аксессуары")]),
    ]
    out = []
    for bucket, title, pieces in plans:
        clean = [p for p in pieces if p]
        if clean:
            out.append({"bucket": bucket, "title": title, "items": clean[:4]})
    return out


# Пороги температуры для объяснений. Границы бытовые, а не метеорологические: клиентка думает
# «жарко / тепло / прохладно», а не в градусах.
_TEMP_BANDS = [
    (26, "жарко", "лёгкие ткани и открытая обувь, верхний слой не нужен"),
    (18, "тепло", "один слой, лёгкий жакет или рубашка сверху по желанию"),
    (10, "прохладно", "нужен полноценный верхний слой и закрытая обувь"),
    (2, "холодно", "плотный слой, шерсть или кашемир, закрытая обувь"),
    (-99, "мороз", "тёплое пальто или пуховик, многослойность обязательна"),
]

# Что роль требует от силуэта. Это и есть explainable-слой: клиентка видит, ПОЧЕМУ образ такой.
_ROLE_CHIP = {
    "Работа": "собранный силуэт и чёткая линия плеча",
    "Выход": "акцентная деталь и более нарядная фактура",
    "Повседневное": "свободнее в крое, упор на комфорт",
}

_MOOD_CHIP = {
    "властная": "структура и контраст держат авторитет",
    "открытая": "мягкая линия и светлый верх ближе к лицу",
    "элегантная": "чистая линия без лишнего декора",
    "дорогая": "качество ткани важнее количества деталей",
    "женственная": "мягкий силуэт с обозначенной талией",
    "незаурядная": "одна выразительная деталь на спокойной базе",
}


def _temp_band(temp) -> tuple[str, str] | None:
    try:
        t = float(temp)
    except (TypeError, ValueError):
        return None
    for edge, label, advice in _TEMP_BANDS:
        if t >= edge:
            return label, advice
    return None


def outfit_chips(weather: dict | None, role: str | None, mood: str | None) -> list[dict]:
    """Объяснения под образом дня: почему он собран именно так.

    Считается кодом, а не моделью: клиентка должна видеть связь «условие → следствие», и эта
    связь обязана быть одинаковой при одинаковых входных данных. Каждый чип — {label, why}.
    """
    chips: list[dict] = []
    if weather:
        band = _temp_band(weather.get("temp"))
        if band:
            label, advice = band
            temp = weather.get("temp")
            chips.append({"label": f"{round(float(temp))}° — {label}", "why": advice})
        if weather.get("is_rain"):
            chips.append({"label": "дождь", "why": "плотная обувь и верхний слой, замша сегодня не идёт"})
        elif weather.get("is_snow"):
            chips.append({"label": "снег", "why": "закрытая обувь на устойчивой подошве"})
        if (weather.get("wind") or 0) >= 8:
            chips.append({"label": "ветрено", "why": "верхний слой с плотной посадкой, лёгкий шёлк развевается"})
    if role:
        why = _ROLE_CHIP.get(role)
        if why:
            chips.append({"label": f"роль: {role.lower()}", "why": why})
    if mood:
        why = _MOOD_CHIP.get((mood or "").strip().lower())
        if why:
            chips.append({"label": f"настроение: {mood.lower()}", "why": why})
    return chips


def _daily_week_view(card: dict, board: list[dict], weekday: int | None = None) -> dict | None:
    """Практический слой кабинета: что надеть сегодня и как выглядит ритм недели."""
    looks = [lk for lk in (card.get("looks") or []) if lk.get("scenario") or lk.get("name")]
    if not looks and not board:
        return None

    weekday = datetime.now().weekday() if weekday is None else weekday
    day_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_long = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
    bucket_priority = {
        "Работа": [lk for lk in looks if lk.get("bucket") == "Работа"],
        "Повседневное": [lk for lk in looks if lk.get("bucket") == "Повседневное"],
        "Выход": [lk for lk in looks if lk.get("bucket") == "Выход"],
    }
    bucket_cycle = ["Работа", "Работа", "Повседневное", "Работа", "Выход", "Повседневное", "Повседневное"]

    board_week = _board_week_outfits(board)
    fallback_items = ((board_week[0]["items"] if board_week else []) or [])[:3]

    def _pick(bucket: str, used: set[int]) -> dict | None:
        for idx, lk in enumerate(looks):
            if idx in used:
                continue
            if lk.get("bucket") == bucket:
                used.add(idx)
                return lk
        for idx, lk in enumerate(looks):
            if idx in used:
                continue
            used.add(idx)
            return lk
        return None

    used: set[int] = set()
    week = []
    for i, day in enumerate(day_labels):
        lk = _pick(bucket_cycle[i], used)
        board_row = board_week[i] if i < len(board_week) else None
        title = ((board_row or {}).get("title")
                 or (lk or {}).get("scenario") or (lk or {}).get("name") or bucket_cycle[i])
        pieces = ((board_row or {}).get("items") or (lk or {}).get("items") or fallback_items)[:4]
        text = (lk or {}).get("why_it_works") or (
            f"Опора на {bucket_cycle[i].lower()} сценарий: собери день из уже согласованных вещей капсулы."
        )
        # фото образа — из уже готовых образов Карты, чтобы неделя была лентой кадров, а не
        # списком текста. Новых генераций не запускаем: кадр стоит ~30с и денег на ключе.
        week.append({"day": day, "title": title.capitalize(), "text": text, "tags": pieces,
                     # bucket нужен объяснениям: заголовок дня — это сценарий («деловая встреча»),
                     # а требование к силуэту задаёт именно роль (Работа / Выход / Повседневное).
                     "bucket": bucket_cycle[i], "img": (lk or {}).get("img")})

    today_row = week[weekday % 7]
    return {
        "today": {
            "title": f"Сегодня — {today_row['title'].lower()}",
            "body": (
                f"Не начинай {day_long[weekday % 7]} с случайного выбора. Возьми уже согласованный "
                f"сценарий и собери день из вещей, которые поддерживают твою Формулу."
            ),
            "items": today_row["tags"],
            "bucket": today_row["bucket"],
            "cta": "Собрать образ из капсулы",
        },
        "week": week,
    }


@app.get("/cabinet")
def cabinet():
    """Кабинет: капсульный гардероб по сезонам + конструктор образов (верх/низ/обувь) + лист покупок."""
    email = _current_user()  # кабинет доступен без почты: путь после Карты не должен обрываться
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        # страховка: анонимный диагноз квиза (last_job) привязываем к почте, иначе петля на квиз
        last_job = session.get("last_job")
        job_diag = ((_JOBS.get(last_job) or {}).get("diag") or _load_pending_diag(last_job)) if last_job else None
        if job_diag:
            save_diagnosis(email, job_diag)
            diag = job_diag
        else:
            return render_template_string(
                NEED_DIAGNOSIS, eyebrow="Шаг 1 из 3",
                title="Кабинет открывается после диагностики",
                lead="«Стиль каждый день» продолжает твою Формулу, а не заводит её заново. "
                     "Пройди диагностику — дальше кабинет соберётся сам.")
    by_season = current_card_by_season(email)  # {код_сезона: карта} — последняя версия на сезон
    card = prof.get("card") or {}
    sel = (request.args.get("season") or "").strip()
    if sel in by_season:               # выбран сезон с собранной капсулой — показываем её
        card = by_season[sel]
    elif sel in _CARD_SEASONS:         # сезон валиден, но капсула на него ещё не собрана:
        sel = sel                      # оставляем выбор, визуальная капсула подберётся из каталога
    else:
        sel = card.get("season") or _DEFAULT_SEASON
    if not card:
        return redirect("/card")       # капсулы ещё нет — сначала собрать Карту
    card = _refresh_card_projection(card, diag)
    items_n = 6 if request.args.get("items") == "6" else 12  # капсула 6 / расширенная 12
    # Капсула у Карты и кабинета должна быть ОДНА. Раньше кабинет всегда собирал свой набор из
    # каталога, а Карта — из образов клиентки: на соседних экранах стояли разные вещи, и капсула
    # выглядела случайной. Для собранного сезона берём опору из Карты, каталогом только добираем
    # до нужного размера. Для несобранного сезона опоры ещё нет — там каталог как раньше.
    catalog_board = _visual_capsule(card, diag, items_n)
    own = card.get("starter_capsule") or []
    if own and (card.get("season") or _DEFAULT_SEASON) == sel:
        board = _merge_boards(_capsule_board(own), catalog_board, items_n)
    else:
        board = catalog_board or card.get("capsule_board") or \
            _capsule_board(card.get("base_capsule") or [])
    # Показываем ВСЕ 4 сезона (было — только собранные, у клиентки их 2). Несобранные помечаем
    # флагом built=False: капсула на них подберётся из каталога под палитру/фигуру/стиль.
    season_tabs = [{"code": s, "label": _CARD_SEASONS[s]["label"],
                    "on": s == sel, "built": s in by_season}
                   for s in _SEASON_ORDER]
    palette = [p for p in (card.get("palette") or []) if p.get("hex")]
    # трекер разрыва прямо в дашборде (раньше жил только в /me): точки-замеры + дельта при ≥2
    track = gap_progress(email)
    days_since = None
    if track:
        for p in track["points"]:
            p["date"] = _ru_date(p["ts"])
        last_ts = track["points"][-1].get("ts")
        try:  # сколько дней прошло с последнего замера → мягкий призыв к пере-замеру
            # timezone берём локально, datetime уже импортирован модулем: повторный локальный
            # импорт делал имя локальным на всю функцию и ронял обращения к нему выше по коду
            from datetime import timezone
            dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - dt).days
        except Exception:  # noqa: BLE001 — битый ts не должен ронять кабинет
            days_since = None
    # KPI капсулы: вещей и оценка числа образов (комбинаторика низ×верх + платья как отд. образы)
    def _slot_n(name: str) -> int:
        return sum(len(g["items"]) for g in board if g["slot"] == name)
    n_items = sum(len(g["items"]) for g in board)
    tops, bottoms = _slot_n("Верх"), _slot_n("Низ")
    dresses = _slot_n("Платья и комбинезоны")
    combos = tops * bottoms + dresses
    combos_label = f"{combos}+" if combos >= 12 else (str(combos) if combos else "—")
    gap_now = card.get("gap")
    if track and track.get("points"):
        gap_now = track["points"][-1]["gap"]
    want3 = (diag.get("want_traits_top3") or [])[:3]
    # Эволюция капсулы: что изменилось относительно капсулы «родного» сезона Карты. Раньше при
    # переключении сезона клиентка просто видела другой набор вещей — без объяснения, что ушло
    # и что это дало. Капсула выглядела случайной, а не живой.
    season_diff = None
    home_season = card.get("season") or _DEFAULT_SEASON
    if sel != home_season:
        base_items = [it for grp in (_capsule_board(card.get("starter_capsule") or []) or [])
                      for it in grp.get("items") or []]
        now_items = [it for grp in board for it in grp.get("items") or []]
        if base_items and now_items:
            d = capsule_diff(base_items, now_items, sel)
            if d["changed"]:
                d["from_label"] = _CARD_SEASONS.get(home_season, {}).get("label", home_season)
                d["to_label"] = _CARD_SEASONS.get(sel, {}).get("label", sel)
                season_diff = d
    # Для несобранного сезона берём только текущую каталожную капсулу. Иначе в летнем кабинете
    # показывались осенние generated-образы: палитра и вещи уже летние, а фото и роли — старые.
    use_generated_looks = (card.get("season") or _DEFAULT_SEASON) == sel
    view_card = dict(card)
    if not use_generated_looks:
        view_card["looks"] = []
    # «Роли твоей недели»: по одному образу на жизненную капсулу (Работа/Повседневное/Выход)
    roles = []
    seen_buckets = set()
    for lk in (view_card.get("looks") or []):
        b = lk.get("bucket") or "Повседневное"
        if b in seen_buckets:
            continue
        seen_buckets.add(b)
        roles.append({"bucket": b, "scenario": lk.get("scenario") or b,
                      "name": lk.get("name"), "pieces": (lk.get("items") or [])[:4],
                      "img": lk.get("img")})  # сгенерированный образ на клиентке — фото в карточку роли
    if not roles:
        roles = _board_role_cards(board)
    roles.sort(key=lambda r: ["Работа", "Повседневное", "Выход"].index(r["bucket"])
               if r["bucket"] in ("Работа", "Повседневное", "Выход") else 9)
    # «Прогресс-вехи»: старт, текущий, лучший разрыв, суммарная дельта (из трекера)
    milestones = None
    if track and track.get("points"):
        gaps = [p["gap"] for p in track["points"]]
        milestones = {"start": gaps[0], "now": gaps[-1], "best": min(gaps),
                      "delta": (gaps[0] - gaps[-1]) if len(gaps) > 1 else 0,
                      "count": track.get("measurements", len(gaps))}
    advice = _daily_cabinet_advice(view_card, diag, track, board, card.get("shopping") or [])
    weekview = _daily_week_view(view_card, board)
    # Погода: совет «что надеть сегодня» без неё одинаков в +25 и в −10. Город клиентка задаёт
    # сама, он живёт в профиле. Нет города или ключа OpenWeatherMap — блок просто не показываем.
    city = ((get_profile(email) or {}).get("style_profile") or {}).get("city") or ""
    weather = get_weather(city) if city else None
    dress = dress_advice(weather) if weather else {}
    look_today = _look_of_the_day(view_card, weather)  # фото только если сезон уже собран визуально
    mine = wardrobe_items(email)  # личные вещи: что клиентка забрала после «брать / не брать»
    return render_template_string(
        CABINET_PAGE, email=email, roles=roles, milestones=milestones,
        formula=card.get("formula") or diag.get("style_formula"),
        colortype=_colortype_label(diag.get("colortype")), figure=_figure_short(diag.get("figure_type")),
        want_traits=want3, days_since=days_since, thanks=(request.args.get("fb") == "1"),
        gap=card.get("gap"), gap_now=gap_now, track=track,
        advice=advice, weekview=weekview,
        city=city, weather=weather, dress=dress, weather_on=weather_configured(),
        chips=outfit_chips(weather,
                           (weekview or {}).get("today", {}).get("bucket"),
                           (want3 or [None])[0]),
        look_today=look_today, mine=mine,
        season_label=(card.get("season_label") or (_CARD_SEASONS[sel]["label"] if sel in _CARD_SEASONS else None)),
        n_items=n_items, combos_label=combos_label, items_n=items_n,
        # какой день выделить в плане недели — считаем на сервере, чтобы не зависеть от
        # часового пояса браузера
        today_label=["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][datetime.now().weekday()],
        board=board, palette=palette, shopping=card.get("shopping") or [],
        season_tabs=season_tabs, season_diff=season_diff, outfit_cells=_outfit_cells(board), sel_season=sel)


STYLEBOOK_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Персональный Style Book</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;1,500&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 :root{--paper:#F5EFE3;--ink:#1f1d1b;--soft:#4c463f;--muted:#6b645c;--wine:#5D2230;--line:#e3dccf}
 *{box-sizing:border-box}body{font-family:Onest,-apple-system,sans-serif;background:var(--paper);color:var(--ink);margin:0;line-height:1.62}
 .wrap{max-width:820px;margin:0 auto;padding:0 26px 60px}
 .serif{font-family:'Cormorant Garamond',serif}
 h1,h2,h3{font-family:'Cormorant Garamond',serif;font-weight:600;margin:0}
 .bar{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:center;
  padding:12px 0;background:rgba(245,239,227,.92);backdrop-filter:blur(6px);border-bottom:1px solid var(--line)}
 .bar .logo{font-family:'Cormorant Garamond',serif;font-size:20px}
 .bar button{font:inherit;font-size:14px;background:var(--wine);color:#fff;border:0;border-radius:9px;padding:10px 18px;cursor:pointer}
 .cover{padding:64px 0 40px;border-bottom:1px solid var(--line)}
 .cover .k{font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--muted)}
 .cover h1{font-size:clamp(40px,8vw,72px);line-height:1.02;margin:16px 0 0}
 .cover .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:26px}
 .chip{font-size:13px;padding:5px 13px;border:1px solid var(--line);border-radius:999px;color:var(--soft);background:#fff}
 .chip b{color:var(--wine);font-weight:500}
 section{padding:46px 0;border-bottom:1px solid var(--line)}
 .snum{font-family:'Cormorant Garamond',serif;color:var(--wine);font-size:15px}
 .st{font-size:clamp(24px,4vw,34px);margin:4px 0 20px}
 .lead{font-size:18px;color:var(--soft)}
 .gaprow{display:flex;gap:28px;align-items:center;flex-wrap:wrap}
 .ring{position:relative;width:130px;height:130px;flex:0 0 auto}.ring svg{transform:rotate(-90deg)}
 .ring .v{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
 .ring .n{font-family:'Cormorant Garamond',serif;font-size:38px;color:var(--wine);font-weight:600}
 .ring .l{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
 .palgrp{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);margin:18px 0 9px}
 .sw{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px}
 .swatch{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fff}
 .swatch .c{height:52px}.swatch .nm{font-size:11.5px;padding:6px 8px 7px;line-height:1.2}
 .stop .c{position:relative}.stop .c::after{content:"";position:absolute;inset:0;background:linear-gradient(135deg,transparent 46%,rgba(255,255,255,.85) 47%,rgba(255,255,255,.85) 53%,transparent 54%)}
 .sil{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:8px}.sil li{background:#fff;border:1px solid var(--line);border-radius:999px;padding:7px 15px;font-size:14px}
 .capg{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px;margin-top:6px}
 .cap{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff}
 .cap img{width:100%;aspect-ratio:3/4;object-fit:cover;display:block;background:#efe8db}
 .cap .nm{font-size:12px;padding:8px 9px;line-height:1.25}
 .combos{font-family:'Cormorant Garamond',serif;font-size:40px;color:var(--wine)}
 .looks{display:flex;flex-direction:column;gap:26px;margin-top:8px}
 .look{display:grid;grid-template-columns:300px 1fr;gap:22px;align-items:start}
 .look img{width:100%;border-radius:12px;display:block;background:#efe8db}
 .look .rb{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine)}
 .look .rn{font-family:'Cormorant Garamond',serif;font-size:23px;margin:5px 0 8px}
 .look .rd{font-size:15px;color:var(--soft)}
 .look .ri{list-style:none;padding:0;margin:10px 0 0;display:flex;flex-wrap:wrap;gap:6px}
 .look .ri li{font-size:12.5px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 10px;background:#fff}
 .closing{text-align:center;padding:64px 0}.closing .q{font-family:'Cormorant Garamond',serif;font-size:clamp(22px,4vw,32px);max-width:24ch;margin:0 auto;line-height:1.25}
 @media(max-width:640px){.look{grid-template-columns:1fr}}
 @media print{.bar{display:none}section,.look,.cap{break-inside:avoid;page-break-inside:avoid}body{background:#fff}}
</style></head><body>
<div class=wrap>
<div class=bar><span class=logo>Чувство стиля · Style Book</span><button onclick="window.print()">Скачать PDF</button></div>

<div class=cover>
 <div class=k>Персональный Style Book</div>
 <h1>{{ formula }}</h1>
 <div class=chips>
  {% if colortype %}<span class=chip>Цветотип · <b>{{ colortype }}</b></span>{% endif %}
  {% if figure %}<span class=chip>Силуэт · <b>{{ figure }}</b></span>{% endif %}
  {% if gap is not none %}<span class=chip>Разрыв · <b>{{ gap }}%</b></span>{% endif %}
  {% if season_label %}<span class=chip>Сезон · <b>{{ season_label }}</b></span>{% endif %}
 </div>
</div>

{% if dna or gap is not none %}
<section>
 <div class=snum>01</div><h2 class=st>Где ты сейчас и куда идёшь</h2>
 <div class=gaprow>
  {% if gap is not none %}<div class=ring><svg width=130 height=130 viewBox="0 0 130 130">
   <circle cx=65 cy=65 r=52 fill=none stroke="#efe8db" stroke-width=11></circle>
   <circle cx=65 cy=65 r=52 fill=none stroke="var(--wine)" stroke-width=11 stroke-linecap=round stroke-dasharray=327 stroke-dashoffset="{{ (327*(1-gap/100.0))|round(1) }}"></circle>
  </svg><div class=v><span class=n>{{ gap }}%</span><span class=l>индекс · до</span></div></div>{% endif %}
  <p class=lead style="flex:1;min-width:260px">{{ dna or 'Твой образ догоняет то, кем ты становишься. Индекс настройки образа помогает видеть это движение шаг за шагом.' }}</p>
 </div>
</section>
{% endif %}

{% if palette %}
<section>
 <div class=snum>02</div><h2 class=st>Твоя палитра</h2>
 {% for grp, title in [('base','База и нейтрали'),('main','Основные'),('accent','Акценты')] %}
  {% set items = palette|selectattr('group','equalto',grp)|list %}
  {% if items %}<div class=palgrp>{{ title }}</div><div class=sw>{% for p in items %}<div class=swatch><div class=c style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}</div>{% endif %}
 {% endfor %}
 {% if stop_colors %}<div class="palgrp">Не работает на тебя</div><div class="sw stop">{% for p in stop_colors %}<div class=swatch><div class=c style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}</div>{% endif %}
</section>
{% endif %}

{% if silhouettes %}
<section>
 <div class=snum>03</div><h2 class=st>Твои силуэты</h2>
 <ul class=sil>{% for s in silhouettes %}<li>{{ s }}</li>{% endfor %}</ul>
</section>
{% endif %}

{% if board %}
<section>
 <div class=snum>04</div><h2 class=st>Твоя капсула</h2>
 <p class=lead>Реальные вещи под твою Формулу — стилевая основа, из которой собираются все образы.</p>
 <div class=capg>
  {% for grp in board %}{% for it in grp['items'] %}<div class=cap>{% if it.image %}<img src="{{ it.image }}" alt="{{ it.name }}">{% endif %}<div class=nm>{{ it.name }}</div></div>{% endfor %}{% endfor %}
 </div>
 {% if combos %}<p style="margin-top:16px"><span class=combos>{{ combos }}+</span> <span class=lead>образов из этих вещей</span></p>{% endif %}
</section>
{% endif %}

{% if looks %}
<section>
 <div class=snum>05</div><h2 class=st>Твои образы</h2>
 <div class=looks>
  {% for lk in looks %}
  <div class=look>
   <img src="{{ lk.img }}" alt="Образ">
   <div>
    <div class=rb>{{ lk.bucket or lk.scenario or 'Образ' }}</div>
    <div class=rn>{{ lk.name or lk.scenario or 'Готовый образ' }}</div>
    <div class=rd>{{ lk.description or lk.desc or '' }}</div>
    {% if lk['items'] %}<ul class=ri>{% for it in lk['items'] %}<li>{{ it }}</li>{% endfor %}</ul>{% endif %}
   </div>
  </div>
  {% endfor %}
 </div>
</section>
{% endif %}

<div class=closing>
 <p class=q>Твоя сила — не в одной вещи, а в системе: формула, цвет и силуэт, которые работают на то, кем ты становишься.</p>
</div>
</div></body></html>"""


STYLEBOOK_UPSELL = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Персональный Style Book</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Onest:wght@300;400;500&display=swap" rel="stylesheet">
<style>:root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
body{font-family:Onest,-apple-system,sans-serif;background:var(--cream);color:var(--ink);margin:0;line-height:1.6}
.wrap{max-width:640px;margin:0 auto;padding:60px 22px}h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:40px;margin:0 0 10px}
.sub{color:var(--muted);font-size:17px}ul{list-style:none;padding:0;margin:26px 0}li{padding:10px 0 10px 28px;position:relative;border-bottom:1px solid var(--line)}
li::before{content:"✓";position:absolute;left:0;color:var(--wine)}
.btn{display:inline-block;background:var(--wine);color:#fff;text-decoration:none;padding:15px 30px;border-radius:10px;margin-top:12px}
a.back{color:var(--muted);font-size:14px;text-decoration:none;display:inline-block;margin-top:20px}</style></head><body><div class=wrap>
<h1>Твой персональный Style Book</h1>
<p class=sub>Книга с твоими образами на фото — часть пакета «Преображение».</p>
<ul>
 <li>Все образы капсулы — на фото, это ты, а не абстрактная модель</li>
 <li>Формула, палитра, силуэты и капсула в одном премиальном документе</li>
 <li>4 роли твоей недели с готовым составом образа</li>
 <li>PDF к печати — держишь в руках, показываешь в шкафу</li>
</ul>
<a class=btn href="/premium.html">Узнать о «Преображении» →</a><br>
<a class=back href="/cabinet">← Вернуться в живой гардероб</a>
</div></body></html>"""


@app.post("/wardrobe/upload")
def wardrobe_upload():
    """Загрузка своих вещей пачкой: каждое фото → vision-разбор → вещь в гардеробе.

    Это ядро обещания «капсула из ТВОИХ вещей»: клиентка приходит не с пустым шкафом, и
    продукт должен сначала показать, что у неё уже есть, а не сразу продавать покупки.

    Каждая вещь — отдельный vision-вызов, поэтому пачку ограничиваем: и по квоте ключа, и
    потому что разбор 20 фото занимал бы минуты, а клиентка ждёт у экрана.
    """
    email = _current_user()
    diag = (get_profile(email) or {}).get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/cabinet")           # без Формулы вердикт не с чем сверять

    files = [f for f in request.files.getlist("photos") if f and f.filename][:MAX_WARDROBE_UPLOAD]
    if not files:
        return redirect("/wardrobe")

    added, failed = 0, 0
    for f in files:
        if not _quota_left():
            break
        try:
            photo_path = _validate_and_save(f)
        except ValueError:
            failed += 1
            continue
        record_call()
        try:
            v = evaluate_garment(str(photo_path), diag, mode="dev")
        except Exception:  # noqa: BLE001 — одна нераспознанная вещь не валит всю загрузку
            failed += 1
            continue
        name = (v.get("item") or "").strip()[:160]
        if not name:
            failed += 1
            continue
        add_wardrobe_item(email, {
            "name": name,
            "slot": _capsule_slot(name),
            "verdict": (v.get("verdict") or "").strip()[:40],
            "reason": (v.get("reason") or "").strip()[:400],
        })
        added += 1
    record_event("wardrobe_uploaded", email, meta=f"{added}/{len(files)}")
    return redirect(f"/wardrobe?added={added}&failed={failed}")


@app.get("/wardrobe")
def wardrobe_page():
    """Мой гардероб: что оставляем, сколько образов уже собирается, чего не хватает."""
    email = _current_user()
    prof = get_profile(email) or {}
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return render_template_string(
            NEED_DIAGNOSIS, eyebrow="Шаг 1 из 3",
            title="Гардероб разбирается после диагностики",
            lead="Чтобы сказать, что из твоих вещей работает, а что нет, нужна Формула. "
                 "Пройди диагностику — дальше разберём шкаф по ней.")
    items = wardrobe_items(email)
    summary = wardrobe_summary(items)
    card = prof.get("card") or {}
    catalog = card.get("visual_capsule") or card.get("capsule_board") or []
    return render_template_string(
        WARDROBE_PAGE, s=summary, suggestions=wardrobe_suggestions(items, catalog),
        added=request.args.get("added"), failed=request.args.get("failed"),
        limit=MAX_WARDROBE_UPLOAD)


@app.post("/wardrobe/add")
def wardrobe_add():
    """Вещь из проверки «брать / не брать» → в личный гардероб клиентки."""
    email = _current_user()
    name = (request.form.get("name") or "").strip()[:160]
    if name:
        add_wardrobe_item(email, {
            "name": name,
            "slot": _capsule_slot(name),
            "verdict": (request.form.get("verdict") or "").strip()[:40],
            "reason": (request.form.get("reason") or "").strip()[:400],
        })
        record_event("wardrobe_item_added", email)
    return redirect("/cabinet#wardrobe-mine")


@app.post("/wardrobe/remove")
def wardrobe_remove():
    """Передумала — убрать вещь из гардероба."""
    email = _current_user()
    try:
        delete_wardrobe_item(email, int(request.form.get("id") or 0))
    except (TypeError, ValueError):
        pass
    return redirect("/cabinet#wardrobe-mine")


@app.post("/cabinet/city")
def cabinet_city():
    """Город клиентки для погоды. Живёт в профиле — задаётся один раз, дальше совет учитывает погоду."""
    email = _current_user()
    city = (request.form.get("city") or "").strip()[:80]
    prof = get_profile(email) or {}
    sp = dict(prof.get("style_profile") or {})
    sp["city"] = city
    save_style_profile(email, sp)
    return redirect("/cabinet")


@app.get("/stylebook")
def stylebook():
    """Фото-Style Book (пакет «Преображение»): книга из данных Карты с ФОТО образов на клиентке.
    Собирается из готовых выходов движка (палитра/капсула/образы) — без новых генераций.
    За гейтом оплаты (пока — админ / premium-флаг; upsell для остальных)."""
    # Стайлбук платный: анониму показываем не логин, а upsell (гейт ниже) — логин ничего не решает.
    email = _current_user()
    prof = get_profile(email)
    card = prof.get("card") or {}
    diag = prof.get("diagnosis") or {}
    if not card.get("formula"):
        return redirect("/card")
    if not _stylebook_access(email):
        return render_template_string(STYLEBOOK_UPSELL)
    looks = [lk for lk in (card.get("looks") or []) if lk.get("img")]  # только образы с фото
    board = card.get("visual_capsule") or card.get("capsule_board") or []
    palette = [p for p in (card.get("palette") or []) if p.get("hex")]
    return render_template_string(
        STYLEBOOK_PAGE, email=email,
        formula=card.get("formula"), gap=card.get("gap"),
        colortype=_colortype_label(diag.get("colortype")), figure=_figure_label(diag.get("figure_type")),
        dna=card.get("dna") or "", silhouettes=card.get("silhouettes") or [],
        palette=palette, stop_colors=card.get("stop_colors") or [],
        board=board, combos=card.get("combination_count"), looks=looks,
        season_label=card.get("season_label"))


@app.get("/stylist")
def stylist_page():
    return render_template_string(STYLIST_PAGE)


@app.post("/stylist/msg")
def stylist_msg():
    """Один ход диалога со стилистом. Контекст — Формула вошедшей клиентки + RAG."""
    data = request.get_json(silent=True) or {}
    history = data.get("history") if isinstance(data.get("history"), list) else []
    email = _current_user()
    profile = get_profile(email) or None
    # сохраняем последнюю реплику пользователя (переписка со стилистом)
    if history and isinstance(history[-1], dict) and history[-1].get("role") == "user":
        last = history[-1].get("content") or history[-1].get("text") or ""
        record_chat(email, "user", last)
    try:
        reply = stylist_reply(history, profile, mode="dev")
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e), "reply": "Сейчас не получилось ответить — попробуй ещё раз."}), 200
    record_chat(email, "assistant", reply)
    return jsonify({"reply": reply})


def _diag_signature(diag: dict) -> str:
    """Отпечаток диагностики для инвалидации кэша Карты. Берём поля, которые НЕ мутирует
    сборка Карты (refine_substyle меняет формулу/подстиль, но не эти): сам Gap + вектор
    семантического поля + желаемые черты. Новый прогон квиза → другой отпечаток."""
    payload = json.dumps({
        "gap": diag.get("gap_percentage"),
        "field": diag.get("semantic_field_distribution") or {},
        "want": diag.get("want_traits_top3") or [],
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _card_stale(prof: dict) -> bool:
    """Собранная Карта устарела: диагностика в профиле изменилась (клиентка заново прошла квиз),
    а Карта осталась на прежней."""
    card = prof.get("card") or {}
    diag = prof.get("diagnosis") or {}
    if not card or not diag:
        return False
    sig = card.get("_diag_sig")
    if sig:
        return sig != _diag_signature(diag)
    # Старая Карта без отпечатка (собрана до фичи): сверяем Gap напрямую. Если разошёлся с текущей
    # диагностикой — Карта устарела (это и был баг: квиз 44%, а Карта показывала прежние 78%).
    cg, dg = card.get("gap"), diag.get("gap_percentage")
    return cg is not None and dg is not None and cg != dg


# Сезоны капсульного гардероба: 4 кода → строка для генерации + ярлык + порядок для переключателя.
_CARD_SEASONS = {
    "spring": {"label": "Весна 2026", "gen": "весна 2026, межсезонье, лёгкие слои"},
    "summer": {"label": "Лето 2026", "gen": "лето 2026, лёгкие ткани, жара"},
    "autumn": {"label": "Осень 2026", "gen": "осень 2026, многослойность, плотные ткани"},
    "winter": {"label": "Зима 2026–2027", "gen": "зима 2026-2027, тёплый слой, верхняя одежда"},
}
_SEASON_ORDER = ["spring", "summer", "autumn", "winter"]
_DEFAULT_SEASON = "autumn"

# Канонические сценарии продукта «Карта стиля». Генератор может назвать их шире/иначе, поэтому
# после LLM всегда приводим к одной продуктовой шестерке — чтобы клиентка видела стабильный
# результат: деловая встреча, свидание, выходные, презентация, корпоратив, путешествие.
_CARD_SCENARIOS = [
    "деловая встреча", "свидание", "выходные",
    "презентация", "корпоратив", "путешествие",
]
_SCENARIO_ALIASES = {
    "деловая встреча": ["работа", "офис", "встреча", "meeting", "business"],
    "свидание": ["свидание", "date", "romantic"],
    "выходные": ["повседневное", "выходные", "weekend", "casual"],
    "презентация": ["презентация", "presentation", "выступление"],
    "корпоратив": ["событие и выход", "вечер", "event", "корпоратив", "выход"],
    "путешествие": ["путешествие", "travel", "trip", "поездка"],
}
_SCENARIO_EFFECT = {
    "деловая встреча": "собранно и уверенно",
    "свидание": "мягко и притягательно",
    "выходные": "легко и без случайности",
    "презентация": "ясно и статусно",
    "корпоратив": "заметно и уместно",
    "путешествие": "продуманно и комфортно",
}
_SCENARIO_BUCKET = {
    "деловая встреча": "Работа",
    "презентация": "Работа",
    "выходные": "Повседневное",
    "путешествие": "Повседневное",
    "свидание": "Выход",
    "корпоратив": "Выход",
}


def _scenario_match(raw_scenario: str, target: str) -> bool:
    """Сопоставить имя сценария LLM с каноническим продуктовым сценарием."""
    raw = (raw_scenario or "").strip().lower()
    tgt = (target or "").strip().lower()
    if not raw or not tgt:
        return False
    if raw == tgt or raw in tgt or tgt in raw:
        return True
    return any(k in raw for k in _SCENARIO_ALIASES.get(target, []))


def _scenario_tokens(scenario: str) -> list[str]:
    return [scenario] + _SCENARIO_ALIASES.get(scenario, [])


def _scenario_missing_items(look: dict) -> list[str]:
    """Каких блоков не хватает до полного комплекта."""
    items = [str(x).lower() for x in (look.get("items") or [])]

    def _has(*parts: str) -> bool:
        return any(p in it for it in items for p in parts)

    missing = []
    if not (_has("жакет", "пиджак", "блейзер", "кардиган", "тренч", "пальто")
            or _has("плать", "комбинезон")):
        missing.append("верхний слой")
    if not (_has("юбк", "брюк", "джин", "плать", "комбинезон", "шорт")):
        missing.append("низ или платье")
    if not _has("туф", "бот", "лофер", "крос", "балет", "мюл", "сап", "босон"):
        missing.append("обувь")
    if not _has("сум", "клатч", "шоппер", "тоут"):
        missing.append("сумка")
    return missing


def _scenario_why_it_works(look: dict, diag: dict, scenario: str) -> str:
    """Короткое explainable-объяснение без повторения длинного описания."""
    vf = diag.get("visual_formula") or {}
    sil = ((vf.get("silhouettes") or [])[:1] or ["твою фигуру"])[0]
    effect = _SCENARIO_EFFECT.get(scenario, "на твою Формулу")
    # Без подстановки сценария в предложение: он приходит в именительном («деловая встреча»),
    # а шаблон «Под {сценарий} образ…» требует винительного — на карточках выходило
    # «Под деловая встреча образ работает». Сценарий и так стоит заголовком карточки,
    # повторять его в первой же строке незачем.
    return f"Образ работает {effect} — держит {sil}."


def _look_preview_images(looks: list[dict]) -> list[str]:
    """По одному предметному кадру на образ — и по возможности РАЗНОМУ.

    Пока фото на клиентке не сгенерированы, карточка образа показывает вещь из его состава.
    Брать «первую подходящую» нельзя: брюки входят в четыре образа из шести, и лента выглядела
    как одна и та же картинка, размноженная шесть раз. Поэтому сначала раздаём вещи, которые
    ещё никем не заняты, и только если своего не осталось — повторяем.

    Приоритет внутри образа — вещь, которая его определяет: платье и юбка меняют силуэт
    сильнее, чем блузка, верхний слой заметнее обуви.
    """
    weight = {"платье": 0, "юбка": 1, "пальто": 2, "плащ": 3, "тренч": 4, "жакет": 5,
              "джемпер": 6, "блуза": 7, "рубашка": 8, "брюки": 9, "джинсы": 10,
              "ботильоны": 11, "сапоги": 12, "туфли": 13, "лоферы": 14, "сумка": 15}
    used: set[str] = set()
    previews: list[str] = []
    for lk in looks:
        cands = []
        for name in (lk.get("items") or []):
            url = item_image_url(name or "")
            if url:
                cands.append((weight.get(item_type(name), 99), url))
        cands.sort(key=lambda c: c[0])
        pick = next((u for _, u in cands if u not in used), None) or (cands[0][1] if cands else "")
        if pick:
            used.add(pick)
        previews.append(pick)
    return previews


# Тип вещи внутри слота — группы синонимов. Слот слишком широк: «Лодочки» и «Угги» оба Обувь,
# и подбор по слоту ставил под лодочки фото угг. Но и голое сравнение слов не годится: «жакет»
# и «пиджак» — одна вещь, названная по-разному в разных фидах. Поэтому синонимы сведены к
# каноническому типу: жакет находит пиджак, лодочки не находят угги.
_ITEM_KIND_SYNONYMS = {
    "жакет": ("жакет", "пиджак", "блейзер"),
    "пальто": ("пальто", "плащ", "тренч"),
    "куртка": ("куртк", "косух", "бомбер", "ветровк"),
    "шуба": ("шуба", "дублён", "дубленк", "пуховик"),
    "кардиган": ("кардиган", "жилет"),
    "рубашка": ("рубашк", "блуз", "сорочк"),
    "джемпер": ("джемпер", "свитер", "пуловер", "водолазк", "бадлон", "свитшот", "худи"),
    "футболка": ("футболк", "майк", "лонгслив", "поло"),
    "топ": ("топ", "боди", "корсет", "бюстье"),
    "брюки": ("брюк", "палаццо", "чинос", "кюлот", "легинс"),
    "джинсы": ("джинс",),
    "юбка": ("юбк",),
    "шорты": ("шорт",),
    "платье": ("плать", "сарафан"),
    "комбинезон": ("комбинезон",),
    "лодочки": ("лодочк", "туфл"),
    "лоферы": ("лофер", "мокасин", "оксфорд", "броги", "дерби"),
    "ботинки": ("ботильон", "ботинк", "челси"),
    "сапоги": ("сапог", "ботфорт"),
    "угги": ("угг", "дутик"),
    "босоножки": ("босонож", "сандал", "шлепанц", "сланц", "эспадрил"),
    "балетки": ("балетк", "слингбэк", "мюли", "сабо"),
    "кроссовки": ("кроссовк", "кед", "слипон"),
    "сумка": ("сумк", "клатч", "шоппер", "тоут", "рюкзак"),
    "ремень": ("ремен", "пояс"),
    "шарф": ("шарф", "платок", "косынк", "палантин"),
    "украшение": ("кулон", "цепочк", "серьг", "браслет", "брошь"),
    "очки": ("очки",),
    "шляпа": ("шляп", "берет", "кепк", "панам"),
    "перчатки": ("перчатк",),
}


def _item_kind(name: str) -> str:
    """Канонический тип вещи: «жакет», «лодочки», «сумка». Пусто — тип не распознан."""
    n = (name or "").lower()
    for canon, variants in _ITEM_KIND_SYNONYMS.items():
        if any(v in n for v in variants):
            return canon
    return ""


def _look_pieces(item_names: list[str], board: list[dict]) -> list[dict]:
    """Вещи образа с фото из каталога — для визуальной раскладки состава (flat-lay).

    Рядом с образом на клиентке показываем, ИЗ ЧЕГО он собран. Фото ищем в board по ТИПУ вещи
    (лодочки к лодочкам), а не по слоту: подбор по слоту подставлял под «Лодочки» фото угг,
    и раскладка выглядела набором случайных вещей из интернета.

    Нет вещи того же типа — оставляем пустую карточку. Чужое фото под правильным названием
    хуже отсутствующего: клиентка видит вещь, которой в её образе нет.
    """
    by_kind: dict[str, list] = {}
    exact: dict[str, dict] = {}
    for grp in board or []:
        for it in grp.get("items") or []:
            name = (it.get("name") or "").strip()
            if not name or not it.get("image"):
                continue
            rec = {"name": name, "image": it.get("image"), "slot": grp.get("slot"),
                   "kind_img": (it.get("image_kind") or "")}
            exact.setdefault(" ".join(name.lower().split()), rec)
            kind = _item_kind(name)
            if kind:
                by_kind.setdefault(kind, []).append(rec)
    # Чистое предметное фото вперёд: у маркетплейсных кадров «на модели» часто рекламный коллаж
    # с текстом поверх картинки — в раскладке образа он выглядит как вещь с чужого сайта.
    for lst in by_kind.values():
        lst.sort(key=lambda r: 0 if r["kind_img"] == "packshot" else 1)

    used: set[int] = set()
    pieces = []
    for raw in item_names or []:
        name = (raw or "").strip()
        if not name:
            continue
        hit = exact.get(" ".join(name.lower().split()))
        if not hit:
            for cand in by_kind.get(_item_kind(name), []):
                if id(cand) not in used:
                    hit = cand
                    used.add(id(cand))
                    break
        pieces.append({
            "name": _ru_item_name(name),
            "slot": _capsule_slot(name),
            # Фото — иллюстрация ТИПА вещи, а не именно эта модель: помечаем, чтобы не выдавать
            # за конкретный товар (продукт не привязан к фиду бренда).
            "image": (hit or {}).get("image"),
            "image_is_example": bool((hit or {}).get("image")),
        })
    return pieces


def _attach_look_pieces(looks: list[dict], board: list[dict]) -> None:
    """Проставить каждому образу раскладку его вещей с фото (на месте)."""
    for lk in looks or []:
        lk["pieces"] = _look_pieces(lk.get("items") or [], board)


def _enrich_card_looks(looks: list[dict], diag: dict) -> list[dict]:
    """Добавить к образам Карты explainable-слой и стабильные продуктовые поля."""
    out = []
    previews = _look_preview_images(looks)
    for i, lk in enumerate(looks):
        scenario = lk.get("scenario") or ""
        item_names = [it for it in (lk.get("items") or []) if it]
        enriched = dict(lk)
        enriched["scenario"] = scenario
        enriched["bucket"] = _SCENARIO_BUCKET.get(scenario, "Повседневное")
        enriched["effect"] = _SCENARIO_EFFECT.get(scenario, "собранно и уместно")
        enriched["why_it_works"] = _scenario_why_it_works(enriched, diag, scenario)
        missing = _scenario_missing_items(enriched)
        if missing:
            enriched["missing_items"] = missing
        enriched["title"] = enriched.get("title") or enriched.get("name") or scenario.capitalize()
        enriched["items"] = item_names
        enriched["preview_img"] = previews[i] if i < len(previews) else ""
        out.append(enriched)
    return out


def _shop_search_links(query: str) -> dict:
    """Ссылки на ПОИСК вещи по описанию. Не привязываемся к конкретному товару и фиду бренда:
    описание («приталенный жакет в графите») работает в любом магазине и не зависит от наличия,
    цены и договорённостей."""
    q = quote_plus(query or "")
    return {
        "wildberries": f"https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
        "lamoda": f"https://www.lamoda.ru/catalogsearch/result/?q={q}",
        "ozon": f"https://www.ozon.ru/search/?text={q}",
    }


def _style_dna_codes(diag: dict, card_bits: dict) -> list[dict]:
    """Style DNA — 3–5 визуальных кодов клиентки: {code, note}.

    По бизнес-логике тарифов Карта обязана показывать, «из чего складывается стиль». Формула
    («Классика × Минимализм») называет направление, но не объясняет, что именно делает образ её.
    Коды собираем из уже посчитанного — силуэтов, палитры, контраста, фигуры и желаемого
    впечатления. Новых обращений к модели не делаем: всё это уже посчитано диагностикой.
    """
    codes: list[dict] = []
    sil = [s for s in (card_bits.get("silhouettes") or []) if s][:2]
    for s in sil:
        codes.append({"code": s, "note": "силуэт, который держит образ"})

    palette = [p.get("name") for p in (card_bits.get("palette") or []) if p.get("name")][:2]
    if palette:
        codes.append({"code": " · ".join(palette), "note": "цвета, с которых начинается твоя база"})

    tonal = diag.get("tonal_characteristics") or {}
    contrast = {"high": "Высокий контраст", "medium": "Средний контраст",
                "low": "Мягкий контраст"}.get(tonal.get("contrast"))
    if contrast:
        codes.append({"code": contrast, "note": "насколько резко работают сочетания"})

    want = (diag.get("want_traits_top3") or (diag.get("quiz") or {}).get("want_traits_top3") or [])
    want = [w for w in want if w][:2]
    if want:
        codes.append({"code": ", ".join(want).capitalize(), "note": "как ты хочешь считываться"})

    fig = card_bits.get("figure")
    if fig and len(codes) < 5:
        codes.append({"code": fig, "note": "геометрия, под которую подобраны посадки"})
    return codes[:5]


_SEASON_LABEL_SHORT = {"spring": "весне", "summer": "лету", "autumn": "осени", "winter": "зиме"}


def _why_dropped(name: str, to_season: str) -> str:
    """Почему вещь ушла из капсулы при смене сезона. Причина обязана быть конкретной:
    «не по сезону» без объяснения выглядит как произвол алгоритма."""
    if not _season_ok(name, to_season):
        return f"не по {_SEASON_LABEL_SHORT.get(to_season, 'сезону')} — ткань и слой не те"
    if _is_dated(name):
        return "вышла из актуального кроя"
    return "уступила место вещи, которая даёт больше сочетаний"


def capsule_diff(old: list[dict], new: list[dict], to_season: str | None = None) -> dict:
    """Что изменилось в капсуле между сезонами: убрано, добавлено, как изменилась комбинаторика.

    Переключение сезона и раньше пересобирало капсулу, но клиентка видела просто другой набор
    вещей. Не было видно ни что ушло, ни почему, ни что это дало — капсула не выглядела живой,
    она выглядела случайной. Здесь считаем осознанность перехода.
    """
    def _key(it):
        return " ".join(str(it.get("name") or "").lower().split())

    old_by = {_key(i): i for i in (old or []) if _key(i)}
    new_by = {_key(i): i for i in (new or []) if _key(i)}

    removed = [{"name": it.get("name"), "why": _why_dropped(it.get("name") or "", to_season or "")}
               for k, it in old_by.items() if k not in new_by]
    # Для добавленных считаем вклад: на сколько комплектов вещь расширила капсулу.
    kept = [it for k, it in new_by.items() if k in old_by]
    added = []
    for k, it in new_by.items():
        if k in old_by:
            continue
        added.append({"name": it.get("name"),
                      "adds_looks": adds_looks(it.get("name") or "", kept),
                      "why": "закрывает слот " + (it.get("slot") or "капсулы").lower()})

    before, after = _outfit_capacity(old or []), _outfit_capacity(new or [])
    return {
        "removed": removed,
        "added": sorted(added, key=lambda a: -a["adds_looks"]),
        "kept_count": len(kept),
        "combinations_before": before,
        "combinations_after": after,
        "combinations_delta": after - before,
        "changed": bool(removed or added),
    }


# ── Разбор личного гардероба ────────────────────────────────────────────────────────────────
# Клиентка приходит не с пустым шкафом. Главный аргумент продукта — «из твоих вещей уже
# собирается N образов», а не «купи ещё». Вся арифметика здесь детерминированная: LLM отвечает
# за распознавание вещи по фото, решения о капсуле и числа считает код.

# Вердикт vision-проверки → что с вещью делать. Формулировки клиентские: «убрать» звучит как
# приговор шкафу, «не в твоей формуле» — как факт о конкретной вещи.
_WARDROBE_BUCKETS = {
    "take": ("keep", "Работает на формулу"),
    "replace": ("fix", "Работает с оговоркой"),
    "skip": ("drop", "Не в твоей формуле"),
}


def wardrobe_breakdown(items: list[dict]) -> dict:
    """Разложить вещи гардероба на «оставить / доработать / не твоё».

    Вещь без вердикта (добавлена руками, без проверки по фото) попадает в keep: не отбираем у
    клиентки её вещи молча, пока не проверили.
    """
    out = {"keep": [], "fix": [], "drop": []}
    for it in items or []:
        verdict = (it.get("verdict") or "").strip().lower()
        bucket, label = _WARDROBE_BUCKETS.get(verdict, ("keep", "В гардеробе"))
        row = dict(it)
        row["bucket_label"] = label
        row["slot"] = it.get("slot") or _capsule_slot(it.get("name") or "")
        out[bucket].append(row)
    return out


def wardrobe_gaps(keep_items: list[dict], target: int = 9) -> list[dict]:
    """Каких слотов не хватает, чтобы из вещей собиралась капсула.

    Считаем по квотам метода (_capsule_quota): верхов больше, чем низов, верхний слой один,
    обувь и сумка обязательны. Возвращаем только реальные дыры, по убыванию важности.
    """
    quota = _capsule_quota(target)
    have: dict[str, int] = {}
    for it in keep_items or []:
        slot = it.get("slot") or _capsule_slot(it.get("name") or "")
        have[slot] = have.get(slot, 0) + 1
    # Порядок важности: без низа и обуви образ не собрать вообще, аксессуар — завершение.
    priority = ["Низ", "Верх", "Обувь", "Верхний слой", "Аксессуары"]
    gaps = []
    for slot in priority:
        need = quota.get(slot, 0)
        got = have.get(slot, 0)
        if got < need:
            gaps.append({"slot": slot, "have": got, "need": need, "missing": need - got})
    return gaps


def wardrobe_summary(items: list[dict], target: int = 9) -> dict:
    """Сводка по гардеробу: что оставляем, сколько образов уже есть, чего не хватает.

    `looks_now` — главное число продукта: сколько комплектов собирается БЕЗ единой покупки.
    """
    parts = wardrobe_breakdown(items)
    keep = parts["keep"] + parts["fix"]      # «с оговоркой» тоже носится, просто аккуратнее
    gaps = wardrobe_gaps(keep, target)
    return {
        **parts,
        "keep_count": len(parts["keep"]),
        "fix_count": len(parts["fix"]),
        "drop_count": len(parts["drop"]),
        "total": len(items or []),
        "looks_now": _outfit_capacity(keep),
        "gaps": gaps,
    }


def wardrobe_suggestions(items: list[dict], catalog: list[dict] | None = None,
                         limit: int = 2) -> list[dict]:
    """Что докупить: максимум 1-2 вещи, каждая закрывает конкретный пробел.

    Правило метода — не покупки ради покупок: предлагаем только под реальную дыру в слоте и
    показываем, сколько образов вещь добавляет. Нет пробелов — нет предложений.
    """
    parts = wardrobe_breakdown(items)
    keep = parts["keep"] + parts["fix"]
    out = []
    for gap in wardrobe_gaps(keep, 9)[:limit]:
        slot = gap["slot"]
        pick = None
        for grp in catalog or []:
            if grp.get("slot") == slot and grp.get("items"):
                pick = grp["items"][0]
                break
        name = (pick or {}).get("name") or f"{slot.lower()} под твою формулу"
        # Вклад считаем по СЛОТУ, а не по имени: обобщённое «низ под твою формулу» слотом не
        # распознаётся, и вещь показывала бы честные, но бессмысленные +0 образов.
        after = _outfit_capacity(list(keep) + [{"name": name, "slot": slot}])
        # Разные формулировки для пустого слота и для «есть, но мало»: сказать «нет вещи»
        # там, где вещь есть, — прямая неправда, клиентка это видит по своему шкафу.
        if gap["have"] == 0:
            why = f"в гардеробе нет ни одной вещи в слоте «{slot.lower()}» — без неё образ не собрать"
        else:
            why = (f"в слоте «{slot.lower()}» пока {gap['have']} — "
                   f"ещё одна вещь заметно расширит комбинаторику")
        out.append({
            "slot": slot,
            "name": name,
            "image": (pick or {}).get("image"),
            "adds_looks": max(0, after - _outfit_capacity(keep)),
            "why": why,
        })
    return out


# Сколько вещей нужно на один самостоятельный комплект, если НЕ строить капсулу: верх, низ и
# обувь. Это консервативная оценка — сумку и верхний слой не считаем, хотя в жизни они тоже
# докупаются. Занижаем осознанно: лучше скромное честное число, чем красивое завышенное.
_PIECES_PER_STANDALONE_LOOK = 3


def capsule_economics(capsule: list[dict], combos: int | None = None) -> dict | None:
    """Что капсула даёт в деньгах и вещах. Всё считается кодом и проверяется на бумаге.

    Три числа:
    - `cost_per_look` — сколько стоит один собранный образ (вся капсула ÷ число сочетаний).
      Это ответ на «дорого»: вещь покупается один раз, а работает в нескольких образах.
    - `saved_items` — сколько вещей НЕ пришлось купить: чтобы закрыть столько же образов
      отдельными комплектами, нужно было бы по 3 вещи на каждый.
    - `cost_without_capsule` — во что обошлись бы те же образы при той же средней цене вещи.

    Возвращаем None, если считать не из чего: выдуманное число хуже отсутствующего.
    """
    items = [it for it in (capsule or []) if isinstance(it, dict)]
    if not items:
        return None
    looks = combos or _outfit_capacity(items)
    if looks < 1:
        return None

    priced = [it for it in items if isinstance(it.get("price"), (int, float)) and it["price"] > 0]
    total = sum(int(it["price"]) for it in priced)
    standalone_items = looks * _PIECES_PER_STANDALONE_LOOK
    # Цену показываем, только если она известна у БОЛЬШИНСТВА вещей капсулы. Иначе сумма делится
    # на все образы и выходит абсурд: на проде цена нашлась у одной вещи из девяти, и клиентка
    # увидела «378 ₽ стоит один собранный образ». Неполные данные хуже отсутствующих.
    enough_prices = len(priced) >= max(2, round(len(items) * 0.6))

    # Денежную «экономию» (средняя цена × вещи, которые не купили) сознательно НЕ считаем: на
    # реальной капсуле выходило больше миллиона рублей. Арифметика верна, но стоит на двойном
    # допущении — что клиентка купила бы все эти вещи и по той же средней цене. Такое число
    # рассыпается от первого вопроса «откуда миллион» и подрывает доверие к остальным.
    return {
        "items": len(items),
        "looks": looks,
        "total": total,
        "cost_per_look": round(total / looks) if (total and enough_prices) else 0,
        "saved_items": max(0, standalone_items - len(items)),
        "standalone_items": standalone_items,
        "has_prices": enough_prices,
    }


def _current_capsule(user: str) -> list[dict]:
    """Опорная капсула пользователя, если Карта уже собрана. Нет Карты — пустой список,
    и метрика «+N образов» просто не показывается вместо того, чтобы врать числом."""
    card = (get_profile(user) or {}).get("card") or {}
    return card.get("starter_capsule") or card.get("base_capsule") or []


def _outfit_capacity(capsule: list[dict]) -> int:
    """Сколько комплектов даёт капсула: верх×низ + платья. Комбинаторика, а не оценка на глаз."""
    by_slot: dict[str, int] = {}
    for it in capsule or []:
        slot = it.get("slot") or _capsule_slot(it.get("name") or "")
        by_slot[slot] = by_slot.get(slot, 0) + 1
    return by_slot.get("Верх", 0) * by_slot.get("Низ", 0) + by_slot.get("Платья и комбинезоны", 0)


def adds_looks(item_name: str, capsule: list[dict]) -> int:
    """Сколько НОВЫХ образов добавит вещь к капсуле.

    Главный аргумент рекомендации: не «эта вещь тебе подойдёт», а «она даёт +6 образов».
    Считаем детерминированно — разницей комбинаторики до и после. Число воспроизводимо и
    проверяемо, в отличие от оценки модели.
    """
    if not item_name:
        return 0
    before = _outfit_capacity(capsule)
    after = _outfit_capacity(list(capsule or []) + [{"name": item_name,
                                                    "slot": _capsule_slot(item_name)}])
    return max(0, after - before)


def _capsule_combos(capsule: list[dict], limit: int = 6) -> list[dict]:
    """Готовые комплекты из капсулы-ядра: верх + низ (+ верхний слой, обувь, сумка).

    Метод продаёт «мало вещей → много образов», но цифра сочетаний без примеров остаётся
    обещанием. Здесь мы показываем, КАК именно они собираются — из тех же вещей ядра.
    """
    by_slot: dict[str, list] = {}
    for it in capsule or []:
        by_slot.setdefault(it.get("slot") or "", []).append(it)
    tops = by_slot.get("Верх") or []
    bottoms = by_slot.get("Низ") or []
    dresses = by_slot.get("Платья и комбинезоны") or []
    layer = (by_slot.get("Верхний слой") or [None])[0]
    shoes = by_slot.get("Обувь") or []
    bags = by_slot.get("Аксессуары") or []

    combos: list[dict] = []
    for i, bottom in enumerate(bottoms):
        for j, top in enumerate(tops):
            pieces = [top, bottom]
            if layer and (i + j) % 2 == 0:
                pieces.append(layer)
            if shoes:
                pieces.append(shoes[(i + j) % len(shoes)])
            if bags and len(pieces) < 5:
                pieces.append(bags[(i + j) % len(bags)])
            combos.append({
                "items": pieces,
                "title": " + ".join(p["name"] for p in pieces[:2]),
                "summary": " · ".join(p["name"] for p in pieces[:4]),
            })
    for k, dress in enumerate(dresses):  # платье — готовый образ, добавляем обувь
        pieces = [dress] + ([shoes[k % len(shoes)]] if shoes else [])
        combos.append({
            "items": pieces,
            "title": dress["name"],
            "summary": " · ".join(p["name"] for p in pieces[:4]),
        })
    return combos[:limit]


# Роль образа по его составу. Слой собирает силуэт и добавляет статуса, платье — готовый выход,
# голая пара «верх + низ» живёт в повседневном. Это правило метода, а не украшение: клиентка
# должна видеть не «ещё одно сочетание», а куда его надеть.
_MATRIX_ROLE = {
    ("pair", True): ("Работа", "слой держит собранный силуэт"),
    ("pair", False): ("Повседневное", "без слоя — легче и свободнее"),
    ("dress", True): ("Выход", "платье со слоем читается статуснее"),
    ("dress", False): ("Свидание", "платье само по себе — готовый образ"),
}


def build_outfit_matrix(capsule: list[dict], max_bases: int = 6) -> dict | None:
    """Матрица «база × слой»: из каких вещей собираются образы.

    Капсула списком вещей читается как шопинг-лист. Матрица показывает то, ради чего она
    собрана: одни и те же вещи в разных сочетаниях дают разные образы. Строки — базы образа
    (верх + низ либо платье), колонки — со слоем и без. Каждая ячейка это готовый комплект.

    Считается кодом из существующей капсулы: ни одной генерации, ни одного вызова модели.
    """
    by_slot: dict[str, list] = {}
    for it in capsule or []:
        by_slot.setdefault(it.get("slot") or _capsule_slot(it.get("name") or ""), []).append(it)

    tops = by_slot.get("Верх") or []
    bottoms = by_slot.get("Низ") or []
    dresses = by_slot.get("Платья и комбинезоны") or []
    layers = by_slot.get("Верхний слой") or []
    shoes = by_slot.get("Обувь") or []
    bags = by_slot.get("Аксессуары") or []

    bases: list[dict] = []
    for bottom in bottoms:
        for top in tops:
            bases.append({"kind": "pair", "items": [top, bottom],
                          "label": f"{top['name']} + {bottom['name']}"})
    for dress in dresses:
        bases.append({"kind": "dress", "items": [dress], "label": dress["name"]})
    if not bases:
        return None
    bases = bases[:max_bases]

    # Колонки: без слоя всегда, плюс каждый верхний слой капсулы. Слоёв в капсуле 1-2 — больше
    # колонок сделало бы таблицу нечитаемой на телефоне.
    columns = [{"label": "Без слоя", "item": None}]
    for lay in layers[:2]:
        columns.append({"label": lay["name"], "item": lay})

    rows = []
    for i, base in enumerate(bases):
        cells = []
        for j, col in enumerate(columns):
            pieces = list(base["items"])
            if col["item"]:
                pieces.append(col["item"])
            # Обувь и сумка — из правила метода: они держат образ и подбираются в тон друг другу,
            # поэтому берём их одной парой по одному индексу, а не двумя независимыми.
            pair_idx = (i + j)
            if shoes:
                pieces.append(shoes[pair_idx % len(shoes)])
            if bags:
                pieces.append(bags[pair_idx % len(bags)])
            role, why = _MATRIX_ROLE[(base["kind"], bool(col["item"]))]
            cells.append({
                "items": [p["name"] for p in pieces],
                "images": [p.get("image") for p in pieces if p.get("image")][:4],
                "role": role,
                "why": why,
            })
        rows.append({"base": base["label"], "kind": base["kind"], "cells": cells})

    return {
        "columns": [c["label"] for c in columns],
        "rows": rows,
        "total": len(rows) * len(columns),
        # НЕ «items»: Jinja резолвит matrix.items в метод словаря, и в Карту попадал
        # «<built-in method items of dict>» вместо числа.
        "items_count": len(capsule or []),
    }


def _core_capsule_from_looks(looks: list[dict], board: list[dict]) -> list[dict]:
    """Капсула-ядро ИЗ ОБРАЗОВ клиентки, а не отдельным набором из каталога.

    Правило продукта (бизнес-логика тарифов, 19.07.2026): капсула Карты не должна быть случайной
    пачкой вещей рядом с образами. Она собирается из того, что реально надето в шести образах:
    вещь, которая работает в нескольких сценариях, и есть ядро гардероба. Иначе клиентка видит
    образы отдельно, капсулу отдельно и не понимает, откуда она взялась.

    Каталог (board) используем только чтобы подтянуть фото и ссылку к вещи по названию.
    """
    if not looks:
        return []
    # Индекс каталога: и по полному имени, и по СЛОТУ. Точного совпадения строк почти не бывает —
    # образ говорит «Пиджак структурный», каталог «Приталенный однобортный жакет». Из-за этого
    # карточки капсулы оставались без фото. Подбираем по слоту: вещь того же типа с фото лучше,
    # чем пустая рамка.
    cat: dict[str, dict] = {}
    by_slot: dict[str, list] = {}
    for grp in board or []:
        for it in grp.get("items") or []:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            rec = {**it, "slot": grp.get("slot")}
            cat.setdefault(" ".join(name.lower().split()), rec)
            by_slot.setdefault(grp.get("slot") or "", []).append(rec)

    used_photos: set[str] = set()

    slot_kind_markers = {
        "Обувь": ("туф", "лодоч", "лофер", "сапог", "ботил", "босонож", "мюл", "балет", "кроссов", "сандал"),
        "Верхний слой": ("жакет", "пиджак", "кардиган", "тренч", "пальто", "куртк", "жилет"),
        "Верх": ("рубаш", "блуз", "топ", "джемпер", "свитер", "футбол", "майк", "водолаз"),
        "Низ": ("брюк", "джинс", "юбк", "шорт"),
        "Платья и комбинезоны": ("плать", "сарафан", "комбинез"),
        "Аксессуары": ("сумк", "ремен", "шарф", "платок"),
    }

    def _kind_tokens(text: str, slot: str) -> set[str]:
        hay = (text or "").lower()
        return {m for m in slot_kind_markers.get(slot, ()) if m in hay}

    def _photo_for(name: str, slot: str) -> dict:
        exact = cat.get(" ".join(name.lower().split()))
        if exact and (exact.get("image_kind") or "") == "packshot":
            return exact
        words = {w for w in re.findall(r"[а-яёa-z]{4,}", name.lower())}
        kind = _kind_tokens(name, slot)
        best = None
        for cand in by_slot.get(slot, []):
            if (cand.get("url") or "") in used_photos:
                continue
            if (cand.get("image_kind") or "") != "packshot":
                continue
            cand_kind = _kind_tokens(cand.get("name") or "", slot)
            if kind and cand_kind and not (kind & cand_kind):
                continue
            cw = {w for w in re.findall(r"[а-яёa-z]{4,}", (cand.get("name") or "").lower())}
            score = len(words & cw)
            if best is None or score > best[0]:
                best = (score, cand)
        # Совпадение хотя бы по одному значимому слову — обязательно. Раньше бралась ЛЮБАЯ вещь
        # того же слота, даже с нулевым сходством: «Блузка с бантом, чёрная» получала фото
        # клетчатой рубашки, «Сумка структурированная, чёрная» — зелёный рекламный коллаж
        # «ТРЕНД 2025», «Приталенный жакет, красный» — белую вещь. Чужая картинка рядом с
        # названием хуже, чем её отсутствие: ниже подставится наш предметный кадр по типу вещи.
        if best and best[0] >= 2 and best[1]:
            used_photos.add(best[1].get("url") or "")
            return best[1]
        return {}

    seen: dict[str, dict] = {}
    for lk in looks:
        scenario = (lk.get("scenario") or "").strip()
        for raw in (lk.get("items") or []):
            name = (raw or "").strip()
            if not name or not _is_capsule_worthy(name):
                continue
            key = " ".join(name.lower().split())
            rec = seen.setdefault(key, {"name": name, "slot": _capsule_slot(name),
                                        "scenarios": [], "outfits_count": 0})
            if scenario and scenario not in rec["scenarios"]:
                rec["scenarios"].append(scenario)
            rec["outfits_count"] += 1

    items = []
    for rec in seen.values():
        n = len(rec["scenarios"]) or rec["outfits_count"]
        extra = _photo_for(rec["name"], rec["slot"]) or {}
        # Ссылка ведёт на ПОИСК по описанию вещи, а не на товар из фида: продукт не зависит от
        # договорённостей с брендами, а клиентка ищет вещь по характеристикам в любом магазине.
        # Фото из каталога — только иллюстрация типа вещи, поэтому помечаем его как пример.
        items.append({
            # price переносим вместе с фото: вещь капсулы взята из образа, а иллюстрирует её
            # каталожная вещь того же типа — её цена и есть ОРИЕНТИР стоимости. Без этого
            # экономика капсулы молчала: цены не доезжали, и «сколько стоит образ» не считалось.
            **{k: v for k, v in extra.items() if k in ("image", "brand", "price")},
            "search": _shop_search_links(rec["name"]),
            "image_is_example": bool(extra.get("image")),
            "name": _ru_item_name(rec["name"]),
            "slot": rec["slot"],
            "outfits_count": n,
            # Сценарии теряли по дороге: капсула честно собиралась из образов, но на карточке
            # не было видно, ИЗ КАКИХ. Клиентка смотрела на вещь и не понимала, откуда она —
            # капсула выглядела набором из каталога, хотя им не была.
            "scenarios": rec["scenarios"],
            # «ядро» — вещь работает минимум в двух сценариях; остальное поддерживает образ
            "capsule_role": "core" if n >= 2 else "accent",
            "why": (f"Работает в {n} образах: {', '.join(rec['scenarios'][:3])}."
                    if n >= 2 else
                    f"Держит образ «{rec['scenarios'][0]}»." if rec["scenarios"] else
                    "Поддерживает формулу."),
        })
    # Сначала ядро по повторяемости, затем добираем слоты без дублей сверх разумной квоты.
    items.sort(key=lambda x: (0 if x["capsule_role"] == "core" else 1, -x["outfits_count"], x["name"]))
    quota = _capsule_quota(9)
    slot_counts: dict[str, int] = {}
    chosen: list[dict] = []
    for it in items:
        slot = it.get("slot") or ""
        if slot_counts.get(slot, 0) >= quota.get(slot, 1):
            continue
        chosen.append(it)
        slot_counts[slot] = slot_counts.get(slot, 0) + 1
        if len(chosen) >= 9:
            break
    return chosen


def _starter_capsule_from_board(board: list[dict]) -> tuple[list[dict], int]:
    """Стартовая капсула 9 вещей из реального board каталога.

    Берём вещи слотами, а не «первые 9 подряд»: тогда в капсуле есть структура ядра гардероба,
    а не случайная пачка предметов одной категории.
    """
    if not board:
        return [], 0
    per_slot = {grp.get("slot"): list(grp.get("items") or []) for grp in board}
    order = [
        ("Верхний слой", 1, "core"),
        ("Верх", 3, "core"),
        ("Низ", 2, "core"),
        ("Платья и комбинезоны", 1, "accent"),
        ("Обувь", 1, "core"),
        ("Аксессуары", 1, "accent"),
    ]
    picked = []
    for slot, need, role in order:
        for it in per_slot.get(slot, [])[:need]:
            piece = dict(it)
            piece["slot"] = slot
            piece["capsule_role"] = role
            piece["outfits_count"] = 3 if role == "core" else 2
            piece["why"] = (
                "Собирает базу капсулы и держит формулу."
                if role == "core" else
                "Добавляет характер и расширяет сочетания."
            )
            picked.append(piece)
    # Если какой-то слот бедный, добираем из оставшегося board лучшими вещами.
    if len(picked) < 9:
        seen = {(p.get("name"), p.get("slot")) for p in picked}
        for grp in board:
            for it in grp.get("items") or []:
                key = (it.get("name"), grp.get("slot"))
                if key in seen:
                    continue
                piece = dict(it)
                piece["slot"] = grp.get("slot")
                piece["capsule_role"] = "optional"
                piece["outfits_count"] = 2
                piece["why"] = "Дозакрывает сценарии, которых не хватило в ядре."
                picked.append(piece)
                seen.add(key)
                if len(picked) >= 9:
                    break
            if len(picked) >= 9:
                break
    bottoms = sum(1 for p in picked if p.get("slot") == "Низ")
    tops = sum(1 for p in picked if p.get("slot") == "Верх")
    dresses = sum(1 for p in picked if p.get("slot") == "Платья и комбинезоны")
    combos = max(18, min(24, tops * max(1, bottoms) * 2 + dresses * 2))
    return picked[:9], combos


def _sync_capsule_views(starter: list[dict], fallback: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Один канон капсулы для всех экранов.

    В Карте исторически жили три представления одновременно:
    - `starter_capsule` — то, что показываем клиентке как ядро;
    - `base_capsule` — то, от чего питается кабинет;
    - `capsule_board` — группировка по слотам для конструктора.

    Из-за этого капсула могла быть «из образов» только в одном блоке, а в кабинете и служебных
    полях оставаться старой или каталожной. Канон такой: если ядро уже собрано из образов,
    именно оно и является базовой капсулой продукта. `fallback` нужен только когда ядра ещё нет.
    """
    base = [dict(it) for it in (starter or fallback or []) if isinstance(it, dict) and it.get("name")]
    return base, _capsule_board(base)


def _refresh_card_projection(card: dict, diag: dict) -> dict:
    """Освежить производные блоки старой сохранённой Карты без новой генерации.

    На проде у клиенток уже лежат старые JSON-версии Карты. После починки логики правая колонка
    могла оставаться «из прошлой жизни»: `starter_capsule` и `capsule_combos` брались как есть из
    БД, хотя новые правила требуют собирать ядро ИЗ текущих образов клиентки.

    Здесь не трогаем саму диагностику и не генерируем заново looks. Мы лишь пересчитываем
    производные проекции показа: визуальную капсулу (если её нет), starter capsule и сочетания.
    """
    if not card:
        return card
    out = dict(card)
    board = list(out.get("visual_capsule") or out.get("capsule_board") or [])
    if not board:
        board = _visual_capsule({
            "palette": out.get("palette") or [],
            "stop_colors": out.get("stop_colors") or [],
            "stop_list": out.get("stop_list") or [],
            "season": out.get("season"),
        }, diag, 9)
    looks = out.get("looks") or []
    starter = _core_capsule_from_looks(looks, board) if looks else []
    if starter:
        core_n = sum(1 for it in starter if it.get("capsule_role") == "core")
        combos_n = max(core_n * 3, len(starter) * 2)
    else:
        starter, combos_n = _starter_capsule_from_board(board)
    base_capsule, capsule_board = _sync_capsule_views(starter, out.get("base_capsule") or [])
    out["visual_capsule"] = board
    out["base_capsule"] = base_capsule
    out["capsule_board"] = capsule_board
    out["starter_capsule"] = starter
    out["starter_capsule_count"] = len(starter)
    out["capsule_combos"] = _capsule_combos(starter)
    out["combination_count"] = combos_n
    # Цветотип и фигуру Карта показывала из своего сохранённого снимка, а кабинет — из текущей
    # диагностики. Стоило диагностике обновиться (пере-замер, ручная правка цветотипа), и на
    # соседних экранах одного профиля стояло «Лето натуральное» против «Осень натуральная».
    # Источник правды один — диагностика.
    if diag.get("colortype"):
        out["colortype"] = _colortype_label(diag.get("colortype"))
    if diag.get("figure_type"):
        out["figure"] = _figure_label(diag.get("figure_type"))
    return out


def _board_role_cards(board: list[dict]) -> list[dict]:
    """Роли недели из текущей капсулы, когда для сезона ещё нет новых generated-образов."""
    by_slot = {grp.get("slot") or "": [it.get("name") for it in (grp.get("items") or []) if it.get("name")]
               for grp in board or []}
    plan = [
        ("Работа", "Деловая встреча", ("Верхний слой", "Верх", "Низ", "Обувь")),
        ("Повседневное", "Повседневный день", ("Верх", "Низ", "Обувь", "Аксессуары")),
        ("Выход", "Свидание или выход", ("Платья и комбинезоны", "Обувь", "Аксессуары", "Верхний слой")),
    ]
    roles: list[dict] = []
    for bucket, scenario, slots in plan:
        pieces: list[str] = []
        for slot in slots:
            names = by_slot.get(slot) or []
            if names:
                pieces.append(names[0])
        if pieces:
            roles.append({"bucket": bucket, "scenario": scenario, "name": scenario,
                          "pieces": pieces[:4], "img": None})
    return roles


def _is_provider_out(e: Exception) -> bool:
    """Кончились кредиты или упёрлись в лимит ключа — то есть генерировать сейчас нечем."""
    t = str(e).lower()
    return ("402" in t or "insufficient" in t or "credit" in t
            or "429" in t or "rate limit" in t or "quota" in t)


def build_card_skeleton(diag: dict, season: str | None = None) -> dict:
    """Карта без единого обращения к модели: структура настоящая, тексты — честные заглушки.

    Нужна в двух случаях: кончились кредиты у провайдера и надо проверять механику тарифов, либо
    прогон интерфейса без трат. Всё, что можно взять из диагностики и каталога, берём по-настоящему:
    формула, разрыв, цветотип, фигура, силуэты, стоп-лист, палитра из visual_formula и капсула из
    реального каталога вещей. Выдумывать состав образов и тексты не имеем права — оставляем пусто
    и честно помечаем карту флагом `no_generation`, чтобы интерфейс сказал об этом клиентке.
    """
    season = season if season in _CARD_SEASONS else _DEFAULT_SEASON
    seas = _CARD_SEASONS[season]
    vf = diag.get("visual_formula") or {}
    deep = diag.get("deep_intake") or {}
    taboo = [t.strip() for t in re.split(r"[;,]", deep.get("taboo", "")) if t.strip()]
    stop_list = (vf.get("stop_list") or []) + [t for t in taboo if t not in (vf.get("stop_list") or [])]
    # палитра диагностики — список названий, приводим к формату Карты
    palette = [{"name": str(c), "hex": "", "group": "base"} for c in (vf.get("palette") or []) if c]
    board = _inline_capsule_images(_visual_capsule({"palette": palette, "stop_list": stop_list}, diag, 9))
    starter, combos = _starter_capsule_from_board(board)
    base_capsule, capsule_board = _sync_capsule_views(starter)
    # Состав образа берём из реальных вещей капсулы по слотам сценария — это НЕ выдумка (вещи из
    # каталога под Формулу), а честная раскладка «этот образ = эти вещи». Даёт визуальный flat-lay
    # даже без генерации фото на клиентке: то, что видит жюри, проходя квиз без своего фото.
    week = {w["title"]: w["items"] for w in _board_week_outfits(board)}
    looks = []
    for sc in _CARD_SCENARIOS:
        title = sc.capitalize()
        # сопоставляем сценарий Карты с ближайшим набором недели (совпадение по названию/бакету)
        items = week.get(title) or week.get(sc.capitalize()) or []
        items = [i for i in items if i]
        looks.append({
            "scenario": sc, "bucket": _SCENARIO_BUCKET.get(sc, "Повседневное"),
            "title": title, "items": items, "effect": _SCENARIO_EFFECT.get(sc, ""),
            # match намеренно не проставляем: вещи капсулы подобраны под Формулу скорингом
            # каталога, а текстовый матчер по названиям этого не видит и показал бы заниженные
            # ~28% — на витрине это читается как «плохо», хотя подбор верный.
            "why_it_works": ("Собран из твоей капсулы под этот сценарий."
                             if items else "Образ соберётся, когда включим генерацию."),
        })
    _attach_look_pieces(looks, board)
    return {
        "formula": diag.get("style_formula"),
        "gap": diag.get("gap_percentage"),
        "dna": diag.get("dna_explanation", ""),
        "colortype": _colortype_label(diag.get("colortype")),
        "figure": _figure_label(diag.get("figure_type")),
        "figure_fit": fit_rules_client(diag.get("figure_type")),
        "contrast": _CONTRAST_RU.get((diag.get("tonal_characteristics") or {}).get("contrast"), ""),
        "palette": palette,
        "stop_colors": [],
        "silhouettes": vf.get("silhouettes") or [],
        "base_capsule": base_capsule, "capsule_board": capsule_board, "visual_capsule": board,
        "starter_capsule": starter,
        "starter_capsule_count": len(starter),
        "capsule_combos": _capsule_combos(starter),
        "combination_count": combos,
        "substyles": [x for x in (diag.get("primary_substyle"), diag.get("secondary_substyle")) if x],
        "accent_note": diag.get("accent_note"),
        "want_traits": [t for t in (diag.get("want_traits_top3") or []) if t][:4],
        "style_dna": _style_dna_codes(diag, {"silhouettes": vf.get("silhouettes"),
                                             "palette": palette,
                                             "figure": _figure_label(diag.get("figure_type"))}),
        "looks": looks, "styling": {}, "shopping": [], "budget": {},
        "style_reference": None,
        "stop_list": stop_list,
        "emphasize": deep.get("adv"),
        "personality": {},
        "substyle_rationale": "",
        "season": season,
        "season_label": seas["label"],
        "no_generation": True,   # интерфейс обязан сказать клиентке, что это ещё не полная Карта
        "_diag_sig": _diag_signature(diag),
    }


def build_style_card(diag: dict, season: str | None = None) -> dict:
    """Собрать продукт «Карта стиля» из Формулы: выверенная палитра + 6 образов + секции.
    Два текстовых вызова (палитра + капсула), без рендера картинок. season — ss|fw (капсула
    собирается под сезон); по умолчанию осень-зима."""
    # Выключатель генерации: SENSE_NO_GEN=1 — собираем Карту без модели. Нужен, чтобы проверять
    # механику тарифов и кабинета, когда кредиты у провайдера кончились или тратить их незачем.
    if os.getenv("SENSE_NO_GEN") == "1":
        return build_card_skeleton(diag, season=season)
    season = season if season in _CARD_SEASONS else _DEFAULT_SEASON
    seas = _CARD_SEASONS[season]
    diag_sig = _diag_signature(diag)  # до refine_substyle: отпечаток исходной диагностики квиза
    vf = diag.get("visual_formula") or {}
    deep = diag.get("deep_intake") or {}  # глубокая диагностика из анкеты Карты
    taboo_items = [t.strip() for t in re.split(r"[;,]", deep.get("taboo", "")) if t.strip()]
    price_segment = deep.get("budget") or "middle"  # из анкеты, иначе средний
    # ШАГ 4 метода: психотип (Big Five) уточняет ПОДСТИЛЬ из 25 — платная глубина. Делаем ДО
    # палитры/капсулы/направлений, чтобы они строились уже по уточнённому подстилю. Без big5 — {}.
    substyle_rationale = ""
    try:
        ref = refine_substyle(diag, deep, mode="dev")
    except Exception:  # noqa: BLE001 — уточнение подстиля не должно ронять карту
        ref = {}
    if ref.get("primary_substyle"):
        ref = enforce_substyles(dict(ref))  # шаг 4 тоже обязан держаться канона
        for key in ("primary_substyle", "secondary_substyle", "accent_note", "style_formula"):
            if ref.get(key):
                diag[key] = ref[key]
        substyle_rationale = ref.get("substyle_rationale") or ""
    # Палитра и капсула — на flash (dev): надёжно и быстро. pro@final в проде отдаёт
    # finish_reason=error (нестабилен), поэтому для продукта НЕ используем (2026-06-29).
    try:
        palette = generate_card_palette(diag, mode="dev")
    except Exception as e:  # noqa: BLE001
        # Кредиты кончились — клиентка не должна упираться в пустой экран. Отдаём Карту без
        # генерации: структура, капсула из каталога и честная пометка вместо выдуманных текстов.
        if _is_provider_out(e):
            print(f"[card] генерация недоступна ({e}); собираем Карту без модели", file=sys.stderr)
            return build_card_skeleton(diag, season=season)
        raise
    scenarios = list(_CARD_SCENARIOS)
    gen_req = {"mode": "capsule", "capsule_type": "auto", "season": seas["gen"],
               "scenarios": scenarios, "n_looks": 6, "price_segment": price_segment,
               # Шаг колориста должен доехать до образов: без этого Карта показывала одну
               # палитру, а образы собирались в других цветах — вплоть до тех, что её гасят.
               "palette": palette.get("palette") or [],
               "stop_colors": palette.get("stop_colors") or [],
               "taboos": taboo_items,  # что точно не носит → не предлагаем
               "emphasize": deep.get("adv"),         # достоинство → подчеркнуть
               "balance": deep.get("balance"),       # что уравновесить
               "want_styles": deep.get("want_styles"),  # визуальный выбор стилей → регистр образов
               "lifecircle": deep.get("lifecircle")}  # круг жизни → вес сценариев
    capsule = generate_capsule(diag, gen_req, mode="dev")
    shopping = {}
    try:  # топ покупок со ссылками — не должен ронять карту
        shopping = generate_shopping_list(diag, capsule, price_segment=price_segment, mode="teaser")
    except Exception:  # noqa: BLE001
        shopping = {}
    cap_items = (capsule.get("capsule") or {}).get("items") or []
    styling = {}
    try:  # стилизация: 1 вещь → 2 образа (капсульная логика) — не должна ронять карту
        styling = generate_styling_pair(diag, cap_items, mode="dev")
    except Exception:  # noqa: BLE001
        styling = {}
    looks = _enrich_card_looks(_ensure_n_looks(capsule.get("looks") or [], scenarios, capsule, diag), diag)
    personality = {}
    if deep.get("big5"):  # личность Big Five → живой портрет (не архетип-ярлык)
        try:
            personality = generate_personality_portrait(deep["big5"], diag, mode="dev")
        except Exception:  # noqa: BLE001 — портрет не должен ронять карту
            personality = {}
    protos = _clean_prototypes(diag.get("prototypes") or [])
    # полный стоп-лист = табу метода (vf) + личные табу из анкеты (без дублей).
    # нужен и для отсечения вещей в визуальной капсуле, и для блока «Стоп-лист» Карты.
    stop_list_full = (vf.get("stop_list") or []) + [t for t in taboo_items if t not in (vf.get("stop_list") or [])]
    visual_capsule = _inline_capsule_images(
        _visual_capsule({"palette": palette.get("palette") or [], "stop_list": stop_list_full}, diag, 9)
    )
    # Капсула-ядро собирается ИЗ ОБРАЗОВ клиентки: вещь, работающая в нескольких сценариях, и есть
    # ядро гардероба. Каталожная сборка остаётся запасной — если у образов нет состава вещей.
    starter_capsule = _core_capsule_from_looks(looks, visual_capsule)
    if starter_capsule:
        # Если образы уже собраны, капсула справа обязана быть ИХ ядром, а не соседним каталогом.
        # Лучше показать 6-7 честных вещей из образов, чем добить блок до девяти посторонними.
        core_n = sum(1 for it in starter_capsule if it.get("capsule_role") == "core")
        starter_combos = max(core_n * 3, len(starter_capsule) * 2)
    else:
        starter_capsule, starter_combos = _starter_capsule_from_board(visual_capsule)
    base_capsule, capsule_board = _sync_capsule_views(
        starter_capsule,
        [it for it in cap_items if isinstance(it, dict) and it.get("name")][:9],
    )
    # Раскладка состава образа фото-вещами: связывает «образ ↔ из чего собран ↔ капсула».
    _attach_look_pieces(looks, visual_capsule)
    return {
        "formula": diag.get("style_formula"),
        "gap": diag.get("gap_percentage"),
        "dna": diag.get("dna_explanation", ""),
        # основа — определяется в диагностике ДО цветов/образов (цветотип → палитра, фигура → силуэты)
        "colortype": _colortype_label(diag.get("colortype")),
        "figure": _figure_label(diag.get("figure_type")),
        "figure_fit": fit_rules_client(diag.get("figure_type")),  # посадка/силуэты под фигуру
        "contrast": _CONTRAST_RU.get((diag.get("tonal_characteristics") or {}).get("contrast"), ""),
        "palette": palette.get("palette") or [],
        "stop_colors": palette.get("stop_colors") or [],
        "silhouettes": vf.get("silhouettes") or [],
        # базовая капсула (ядро) обязана совпадать с тем, что мы вывели из образов клиентки.
        "base_capsule": base_capsule,
        "capsule_board": capsule_board,
        # визуальная капсула: реальные вещи каталога с ФОТО (вшиты в data-URL, чтобы жили и в PDF).
        # Берём 9 вещей как честную стартовую капсулу для продукта «Карта стиля».
        "visual_capsule": visual_capsule,
        "starter_capsule": starter_capsule,
        "starter_capsule_count": len(starter_capsule),
        "capsule_combos": _capsule_combos(starter_capsule),
        # Style DNA — визуальные коды клиентки. Формула называет направление, коды объясняют,
        # что именно делает образ её.
        # Субстили и желаемое впечатление — для карточки ДНК: формула называет направление,
        # субстили уточняют его, а черты говорят, ЧТО она хочет транслировать.
        "substyles": [x for x in (diag.get("primary_substyle"), diag.get("secondary_substyle")) if x],
        "accent_note": diag.get("accent_note"),
        "want_traits": [t for t in (diag.get("want_traits_top3") or []) if t][:4],
        "style_dna": _style_dna_codes(diag, {
            # внутри build_style_card словаря `card` ещё нет — силуэты берём из visual_formula
            "silhouettes": vf.get("silhouettes"),
            "palette": palette.get("palette"),
            "figure": _figure_label(diag.get("figure_type")),
        }),
        "combination_count": starter_combos or (capsule.get("capsule") or {}).get("combination_count"),
        "looks": looks,
        "styling": styling,  # {base_item, idea, looks:[…x2]} — рендерятся в воркере
        "shopping": (shopping.get("shopping_items") or [])[:5],
        "budget": shopping.get("budget_estimate") or {},
        "style_reference": protos[0] if protos else None,
        # личные табу из анкеты добавляем в стоп-лист (без дублей)
        "stop_list": stop_list_full,
        "emphasize": deep.get("adv"),  # достоинство — показываем в Карте «что подчёркиваем»
        "personality": personality,  # {portrait, style_implications} или {}
        "substyle_rationale": substyle_rationale,  # «почему этот подстиль из твоей натуры» (шаг 4)
        "season": season,               # ss|fw — под какой сезон собрана капсула
        "season_label": seas["label"],  # человекочитаемо для кабинета
        "_diag_sig": diag_sig,  # отпечаток диагностики → инвалидация кэша при новом квизе
    }


# Big Five: 10 утверждений (2 на черту), шкала согласия 1..5. (черта, reverse?, текст).
# Черта S = устойчивость (вопросы про тревожность реверсивны). Без терминов — для клиентки это «пара вопросов о тебе».
BIG5_QUESTIONS = [
    ("O", False, "Меня тянет к новому и необычному, люблю эксперименты"),
    ("O", False, "Мне важны эстетика, искусство и красота вокруг"),
    ("C", False, "Люблю порядок, планы и довожу начатое до конца"),
    ("C", False, "Я собранная и дисциплинированная"),
    ("E", False, "Среди людей я заряжаюсь, мне комфортно быть заметной"),
    ("E", False, "Легко завожу разговор и проявляю инициативу"),
    ("A", False, "Мне важно заботиться о близких и хранить тёплые отношения"),
    ("A", False, "Я скорее уступлю, чем буду настаивать на своём"),
    ("S", True, "Я часто тревожусь и переживаю по мелочам"),
    ("S", True, "Меня легко выбить из равновесия"),
]


def _score_big5(form) -> dict:
    """Ответы формы (b5_0..b5_9, 1..5) → уровни черт {O/C/E/A/S: high|mid|low}. Мало ответов → {}."""
    sums, counts = {}, {}
    for i, (trait, rev, _) in enumerate(BIG5_QUESTIONS):
        raw = (form.get(f"b5_{i}") or "").strip()
        if not raw.isdigit():
            continue
        v = int(raw)
        if not 1 <= v <= 5:
            continue
        v = 6 - v if rev else v  # реверс для устойчивости
        sums[trait] = sums.get(trait, 0) + v
        counts[trait] = counts.get(trait, 0) + 1
    if sum(counts.values()) < 6:  # слишком мало ответов — не считаем личность
        return {}
    levels = {}
    for trait, total in sums.items():
        avg = total / counts[trait]
        levels[trait] = "high" if avg >= 3.7 else ("low" if avg <= 2.3 else "mid")
    return levels


@app.context_processor
def _big5_ctx():  # вопросы Big Five доступны в шаблоне формы Карты
    return {"big5_questions": list(enumerate(BIG5_QUESTIONS))}


@app.context_processor
def _style_cards_ctx():  # карточки стилей для визуального выбора в анкете Карты
    return {"style_cards": [{"code": c, **v} for c, v in _STYLE_CARDS.items()]}


@app.context_processor
def _colortype_ctx():
    """Опции цветотипа + текущий (определённый ИИ) — для оверрайда в форме Карты.
    Vision ненадёжен по теплу/холоду (осень↔зима), поэтому даём клиентке поправить вручную."""
    cur = None
    try:
        em = _current_user()
        if em:
            cur = ((get_profile(em) or {}).get("diagnosis") or {}).get("colortype")
    except Exception:  # noqa: BLE001
        cur = None
    return {"colortype_options": list(_COLORTYPE_LABEL.items()),
            "current_colortype_code": cur,
            "current_colortype_label": _colortype_label(cur)}


def _save_deep_intake(email: str, form) -> None:
    """Глубокая диагностика из формы Карты (анкета курса) → профиль (diagnosis.deep_intake).
    Тело+возражения + круг жизни + бюджет + личность Big Five. Питает Формулу/стоп-лист/портрет/чат."""
    # поля-«чипсы»: клиентка выбирает кнопками (мультивыбор) → склеиваем в строку.
    # getlist работает и для одного значения, и для нескольких — совместимо со старым текстом.
    deep = {k: ", ".join(dict.fromkeys(v.strip() for v in form.getlist(k) if v.strip()))[:200]
            for k in ("adv", "balance", "taboo", "audience")}
    deep = {k: v for k, v in deep.items() if v}
    # визуальный выбор стилей («какие образы откликаются») → явный сигнал регистра в генерацию
    want_styles = [c for c in form.getlist("want_styles") if c in _STYLE_CARDS]
    if want_styles:
        deep["want_styles"] = want_styles
    lc = {key: int(form.get("life_" + key))
          for key in ("work", "home", "free")
          if (form.get("life_" + key) or "").strip().isdigit()}
    if lc:
        deep["lifecircle"] = lc
    if (form.get("budget") or "").strip() in ("budget", "middle", "premium"):
        deep["budget"] = form.get("budget").strip()
    levels = _score_big5(form)
    if levels:
        deep["big5"] = levels
    # цветотип-оверрайд: клиентка поправила сезон/подтип (vision ненадёжен по теплу/холоду)
    ov = (form.get("colortype_override") or "").strip()
    override_ct = ov if ov in _COLORTYPE_LABEL else None
    if not deep and not override_ct:
        return
    diag = (get_profile(email) or {}).get("diagnosis") or {}
    if deep:
        diag["deep_intake"] = {**(diag.get("deep_intake") or {}), **deep}
    if override_ct:
        diag["colortype"] = override_ct  # перебиваем → палитра пересоберётся под него
    save_diagnosis(email, diag)


def _ensure_n_looks(looks: list, scenarios: list, capsule: dict, diag: dict) -> list:
    """Гарантия ровно по одному образу на каждый сценарий (LLM иногда отдаёт меньше).
    Переиспользуем образы LLM, лишние переназначаем на пустые сценарии, недостающие
    дособираем из вещей капсулы — без дополнительных вызовов модели."""
    def _norm(s):
        return (s or "").strip().lower()

    by_scn, extras = {}, []
    for lk in looks:
        scn = _norm(lk.get("scenario"))
        match = next((s for s in scenarios if _scenario_match(scn, s)), None)
        if match and match not in by_scn:
            by_scn[match] = lk
        else:
            extras.append(lk)

    items = [it.get("name") for it in ((capsule.get("capsule") or {}).get("items") or [])
             if it.get("name")]
    sils = (diag.get("visual_formula") or {}).get("silhouettes") or []
    formula = diag.get("style_formula") or "твоя Формула стиля"

    out = []
    for s in scenarios:
        if s in by_scn:
            lk = by_scn[s]
        elif extras:
            lk = dict(extras.pop(0))
            lk["scenario"] = s  # переназначаем лишний образ на пустой сценарий
        else:  # досборка из капсулы (детерминированно, без вызова)
            lk = {"scenario": s, "name": None, "items": (items or sils)[:4],
                  "description": f"Образ под сценарий «{s}» в формуле «{formula}» — "
                                 f"собран из ключевых вещей твоей капсулы."}
        out.append({"scenario": s, "name": lk.get("name"),
                    "items": lk.get("items"), "description": lk.get("description", ""),
                    "image_generation_prompt": lk.get("image_generation_prompt", ""),
                    "title": lk.get("title"), "img": lk.get("img")})
    return out


def _friendly_gen_error(e: Exception) -> str:
    """Человеческая причина вместо сырого ответа провайдера.

    Раньше на экран уезжало `OpenRouter 402: {"error":{"message":"Insufficient credits"...}}` —
    клиентке это ничего не говорит, а на сцене выглядит как упавший продукт. Полный текст пишем
    в лог сервера, наружу отдаём причину.
    """
    s = str(e).lower()
    if "402" in s or "insufficient" in s or "credit" in s:
        return "Генерация сейчас недоступна — у AI-провайдера закончился лимит."
    if "timeout" in s or "timed out" in s or "read timed out" in s:
        return "AI отвечает дольше обычного."
    if "401" in s or "403" in s or "api key" in s:
        return "Не получилось обратиться к AI-провайдеру."
    return "Сборка не завершилась."


def _card_look_prompt(lk: dict, diag: dict) -> str:
    """Промпт для рендера образа карты на клиентке: промпт из капсулы или досборка."""
    base = (lk.get("image_generation_prompt") or "").strip()
    if base:
        return base
    parts = []
    if lk.get("scenario"):
        parts.append(f"Образ для сценария «{lk['scenario']}»")
    if lk.get("items"):
        parts.append("вещи: " + ", ".join(lk["items"]))
    pal = _palette_names(diag)
    if pal:
        parts.append(f"палитра: {pal}")
    if diag.get("figure_type"):
        parts.append(f"силуэт под фигуру {diag['figure_type']}")
    return ". ".join(parts) or (diag.get("style_formula") or "современный стильный образ")


def _card_job_worker(job_id: str, photo_path: Path, email: str, season: str | None = None) -> None:
    """Фоновая сборка карты + рендер 6 образов на клиентке. Фото удаляем после."""
    try:
        prof = get_profile(email) or {}
        diag = prof.get("diagnosis") or {}
        had_card = bool(prof.get("card"))
        card = build_style_card(diag, season=season)
        # рендерим 6 образов карты + 2 образа стилизации (одна вещь → два образа)
        targets = list(card.get("looks") or []) + list((card.get("styling") or {}).get("looks") or [])

        def _render(lk):
            try:
                return render_look_on_client(str(photo_path), _card_look_prompt(lk, diag),
                                             season=card.get("season"))
            except Exception:  # noqa: BLE001 — один неудавшийся образ не валит карту
                return None

        # Параллелим ОГРАНИЧЕННО. Раньше воркеров было по числу образов — для Карты это 6 образов
        # плюс 2 стилизации = 8 одновременных генераций, каждая держит картинку ~1 МБ и её
        # обработку. Локально проходило, на контейнере Amvera процесс убивало по памяти: задание
        # исчезало из _JOBS и статус становился «unknown» вместо готовой Карты.
        with concurrent.futures.ThreadPoolExecutor(max_workers=RENDER_WORKERS) as ex:
            imgs = list(ex.map(_render, targets))
        for lk, img in zip(targets, imgs):
            if img:
                lk["img"] = img
        ok_imgs = sum(1 for i in imgs if i)

        # Раскладка вещей для пары «одна вещь — два образа». Только для неё: там приём и живёт,
        # а на все шесть образов это удвоило бы расход ключа. Коллаж из каталожных фото собирался
        # из разных источников — разные фоны и рекламный текст поверх вещи; здесь вещи именно те,
        # что в образе, на одном фоне. Сбой раскладки не должен ронять Карту.
        pal = ", ".join(str((c.get("name") if isinstance(c, dict) else c) or "")
                        for c in (card.get("palette") or [])[:3])
        for lk in ((card.get("styling") or {}).get("looks") or []):
            try:
                flat = render_flatlay(lk.get("items") or [], palette=pal,
                                      season=card.get("season"))
                if flat:
                    lk["flatlay"] = flat
            except Exception:  # noqa: BLE001
                pass
        port = (card.get("personality") or {}).get("portrait")
        if port:  # портрет личности — в профиль, чтобы видел чат-стилист
            d2 = (get_profile(email) or {}).get("diagnosis") or {}
            d2["personality_portrait"] = port
            save_diagnosis(email, d2)
        if ok_imgs == 0:
            record_event("card_build_no_images", email, meta="stale" if had_card else "retry")
            if had_card:
                _JOBS[job_id] = {
                    "status": "stale",
                    "error": ("Новые образы пока не собрались. Последняя Карта сохранена, "
                              "а бесплатная попытка не списана.")
                }
                return
            save_card(email, card)  # сохраняем текстовую базу, чтобы клиентка не теряла результат
            _JOBS[job_id] = {
                "status": "retry",
                "error": ("Образы пока не собрались, но текстовая Карта уже сохранена. "
                          "Можно открыть её сейчас или повторить генерацию позже — "
                          "бесплатная попытка не списана.")
            }
            return
        save_card(email, card)  # храним готовые образы, не исходное фото
        record_event("card_built", email)
        record_event("look_generated", email, meta=str(ok_imgs))
        _JOBS[job_id] = {"status": "done"}
    except Exception as e:  # noqa: BLE001
        # Живое демо на сцене: падение генерации не должно выглядеть падением продукта. Если готовая
        # Карта уже есть — предлагаем открыть её (честно, «прошлая», не выдаём за свежую).
        print(f"[card_build] {type(e).__name__}: {e}", file=sys.stderr)  # полный текст — в лог
        has_card = bool((get_profile(email) or {}).get("card"))
        _JOBS[job_id] = {"status": "stale" if has_card else "error",
                         "error": _friendly_gen_error(e)}
    finally:
        try:
            Path(photo_path).unlink()  # фото не храним (Политика)
        except OSError:
            pass


@app.get("/card")
def style_card():
    """Карта стиля. Готовая (кэш) → показываем; иначе форма загрузки фото для сборки.
    ?text=1 — собрать без образов (только текст, синхронно); ?rebuild=1 — пересобрать."""
    email = _current_user()  # почта не обязательна: аноним идёт дальше под своим id
    _attach_quiz_diagnosis(email)
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        # Не редирект: человек нажал «Карта стиля» осознанно и должен понять, почему её пока нет.
        return render_template_string(
            NEED_DIAGNOSIS, eyebrow="Шаг 1 из 3",
            title="Сначала — диагностика",
            lead="Карта стиля строится на твоей Формуле: цветотипе, силуэте и разрыве между тем, "
                 "как ты выглядишь и как хочешь считываться. Без диагностики её не из чего собрать.")
    card = prof.get("card") or {}
    stale = _card_stale(prof)  # диагностика обновилась (новый квиз), а Карта на прежней
    # Новый квиз → НЕ показываем старую Карту (путает: «прошлый результат»). Ведём вперёд: новый Gap
    # + пересборка под свежую диагностику. Только Gap переходит из квиза, дальше собираем заново.
    if stale and not request.args.get("rebuild") and not request.args.get("text"):
        gap = diag.get("gap_percentage")
        notice = ("<b>Твой разрыв обновился по новому квизу"
                  + (f": {gap}%" if gap is not None else "") + ".</b> "
                  "Старая Карта осталась на прежней диагностике. Собери её заново под свежий результат.")
        record_event("card_stale_rebuild_prompt", email)
        return render_template_string(CARD_BUILD_FORM, error=None, notice=notice)
    if card:
        card = _refresh_card_projection(card, diag)
    if card and not request.args.get("rebuild") and not request.args.get("text"):
        return render_template_string(STYLE_CARD, c=card, name=_display_name(email),
                                      figure_short=_figure_short(diag.get("figure_type")),
                                      dna_fields=_dna_fields(diag),
                                      card_link=_card_link_url(email),
                                      matrix=build_outfit_matrix(card.get("starter_capsule") or []),
                                      econ=capsule_economics(card.get("starter_capsule"),
                                                             card.get("combination_count")),
                                      thanks=request.args.get("fb"), stale=False)
    # бесплатная генерация — один раз на email; пересборку/повтор блокируем (защита токенов).
    # Исключение: диагностика реально изменилась (новый квиз) — даём пересобрать Карту под неё.
    if (request.args.get("rebuild") or request.args.get("text")) and not _gen_allowed(email) and not stale:
        if card:
            return render_template_string(STYLE_CARD, c=card, name=_display_name(email),
                                          figure_short=_figure_short(diag.get("figure_type")),
                                          dna_fields=_dna_fields(diag),
                                          card_link=_card_link_url(email),
                                          matrix=build_outfit_matrix(card.get("starter_capsule") or []),
                                          econ=capsule_economics(card.get("starter_capsule"),
                                                                 card.get("combination_count")),
                                          thanks=None)
        return render_template_string(CARD_BUILD_FORM, error=_GEN_LIMIT_MSG), 429
    if request.args.get("text"):  # текстовая карта без образов (синхронно)
        if not _quota_left():
            return render_template_string(CARD_BUILD_FORM, error="Лимит на сегодня исчерпан."), 429
        record_call()
        try:
            card = build_style_card(diag, season=request.args.get("season"))
            save_card(email, card)
            record_event("card_built", email, meta="text")
        except Exception as e:  # noqa: BLE001
            return render_template_string(CARD_BUILD_FORM, error=f"Не удалось собрать: {e}"), 500
        return render_template_string(STYLE_CARD, c=card, name=_display_name(email),
                                      figure_short=_figure_short(diag.get("figure_type")),
                                      dna_fields=_dna_fields(diag),
                                      card_link=_card_link_url(email),
                                      matrix=build_outfit_matrix(card.get("starter_capsule") or []),
                                      econ=capsule_economics(card.get("starter_capsule"),
                                                             card.get("combination_count")))
    record_event("card_form_view", email)
    return render_template_string(CARD_BUILD_FORM, error=None)


def _card_link_url(user: str) -> str:
    """Полный адрес постоянной ссылки на Карту. Пусто — если выдать не смогли."""
    try:
        token = card_link_token(user)
    except Exception:  # noqa: BLE001 — без ссылки Карта работает, ронять её незачем
        return ""
    return (request.url_root.rstrip("/") + "/card/" + token) if token else ""


@app.get("/card/<token>")
def card_by_link(token):
    """Карта по постоянной ссылке — открывается в любом браузере, живёт после пересборки.

    Только чтение. Сессию НЕ подменяем: иначе ссылка равносильна передаче аккаунта — открывший
    её получил бы чужой кабинет и чужие генерации. Поэтому здесь нет ни пересборки, ни отзыва.
    """
    # Токен — token_urlsafe(16). Формат проверяем, чтобы маршрут не отвечал на случайные пути.
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        abort(404)
    owner = user_by_card_token(token)
    card = (get_profile(owner) or {}).get("card") if owner else None
    if not card:
        return render_template_string(
            NEED_DIAGNOSIS, eyebrow="Ссылка не открывается",
            title="Такой Карты у нас нет",
            lead="Ссылка устарела или Карта ещё не собрана. Попроси свежую ссылку — "
                 "или собери свою Карту стиля, это займёт несколько минут."), 404
    diag = (get_profile(owner) or {}).get("diagnosis") or {}
    card = _refresh_card_projection(card, diag)
    record_event("card_link_view", owner)
    html = render_template_string(STYLE_CARD, c=card, name=_display_name(owner),
                                  figure_short=_figure_short(diag.get("figure_type")),
                                  dna_fields=_dna_fields(diag),
                                  matrix=build_outfit_matrix(card.get("starter_capsule") or []),
                                  econ=capsule_economics(card.get("starter_capsule"),
                                                         card.get("combination_count")),
                                  shared=True, thanks=None, stale=False)
    resp = make_response(html)
    # Карта содержит образы на фото клиентки — в поисковой выдаче ей делать нечего.
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.post("/card/build")
def card_build():
    """Старт асинхронной сборки карты с образами на клиентке (фото → рендер → удаление)."""
    email = _current_user()
    _attach_quiz_diagnosis(email)   # диагноз квиза живёт под job_id, а не под пользователем
    prof = get_profile(email) or {}
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return render_template_string(
            NEED_DIAGNOSIS, eyebrow="Шаг 1 из 3", title="Сначала — диагностика",
            lead="Чтобы собрать Карту, нужна твоя Формула стиля.")
    # бесплатная генерация — один раз на email (защита токенов). Исключение — устаревшая Карта
    # (клиентка заново прошла квиз): разрешаем пересобрать под новую диагностику.
    if not _gen_allowed(email) and not _card_stale(prof):
        return render_template_string(CARD_BUILD_FORM, error=_GEN_LIMIT_MSG), 429
    if not _ip_gen_allowed():  # cookie почистили — ловим по IP
        return render_template_string(CARD_BUILD_FORM, error=_IP_LIMIT_MSG), 429
    if not _quota_left():
        return render_template_string(CARD_BUILD_FORM, error="Лимит на сегодня исчерпан."), 429
    if not _consent_ok(request.form):
        return render_template_string(CARD_BUILD_FORM, error="Нужно согласие на обработку и передачу фото."), 400
    record_consent(email, _client_ip(), True, True)
    try:
        photo_path = _validate_and_save(request.files.get("photo"))
    except ValueError as e:
        return render_template_string(CARD_BUILD_FORM, error=str(e)), 400
    _save_deep_intake(email, request.form)  # тело+возражения из анкеты → в Формулу/стоп-лист/чат
    record_call()
    record_generation(email, _client_ip())  # журнал для лимита по устройству и по IP
    season = (request.form.get("season") or "").strip() or None  # ss|fw — сезон капсулы
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "processing"}
    threading.Thread(target=_card_job_worker, args=(job_id, photo_path, email, season),
                     daemon=True).start()
    return render_template_string(CARD_BUILDING, job_id=job_id)


@app.get("/card/status/<job_id>")
def card_status(job_id):
    return jsonify(_JOBS.get(job_id) or {"status": "unknown"})


@app.post("/lead")
def capture_lead():
    """Захват почты на экране результата квиза (лид). Привязываем диагностику из job, согласие на письма."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if "@" not in email or "." not in email:
        return jsonify({"ok": False, "error": "email"}), 400
    job_id = data.get("job_id")
    diag = (_JOBS.get(job_id) or {}).get("diag") if job_id else None
    if diag:
        save_diagnosis(email, diag)   # привязали диагностику из квиза к почте
    record_session(email, diag or {})  # почта с Формулой/Gap → попадёт в список лидов
    if data.get("marketing"):
        record_event("marketing_optin", email)
    record_event("lead_captured", email, meta="quiz")
    # режим теста: почта введена → сразу впускаем, чтобы «Получить Карту» вело в Карту, а не на /login
    if _open_access():
        # переносим всё, что клиентка нажила анонимно (Карта, замеры, гардероб), иначе она
        # оставляет почту — как мы и просим — и теряет собранный результат
        anon = session.get("anon")
        if anon:
            merge_profile(anon, email)
        session["email"] = email
    return jsonify({"ok": True})


@app.post("/card/feedback")
def card_feedback():
    """Отзыв клиентки о Карте (оценка + текст). Питает артефакт «обратная связь» конкурса."""
    email = _current_user()
    try:
        rating = int(request.form.get("rating") or 0) or None
    except ValueError:
        rating = None
    record_feedback(email, rating, request.form.get("text"))
    record_event("feedback_left", email, meta=str(rating or ""))
    # откуда пришли (Карта или кабинет) — возвращаем туда же. Только внутренний путь: защита от
    # открытого редиректа (без схемы и без «//», чтобы не увели на чужой домен).
    nxt = (request.form.get("next") or "").strip()
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = "/card"
    sep = "&" if "?" in nxt else "?"
    return redirect(f"{nxt}{sep}fb=1")


# приборная панель метрик для конкурса — доступ по email основателя ИЛИ ?key=SENSE_METRICS_KEY
_ADMIN_EMAILS = {e.strip().lower() for e in
                 os.getenv("SENSE_ADMIN_EMAILS", "neiroskyai@gmail.com").split(",") if e.strip()}


def _open_access() -> bool:
    """Режим тестирования: ввод почты сразу пускает в кабинет, без клика по письму.

    Зачем: magic-link на телефоне рвёт сессию (клиентка уходит в почту и не возвращается), а пока
    SMTP не починен — письма и вовсе не доходят. На время прогонов клиенток впускаем по вводу почты,
    письмо шлём фоном для возврата потом. ВНИМАНИЕ: без подтверждения почты любой войдёт под чужим
    адресом — это осознанный компромисс на бесплатном lead-уровне (платного контента и чужих ПДн
    там нет). ПО УМОЛЧАНИЮ ВКЛ на время тестов — ПЕРЕД ПУБЛИЧНЫМ ПОСТОМ / платным уровнем задать
    SENSE_OPEN_ACCESS=0 (иначе любой посетитель войдёт под чужой почтой).
    """
    return os.getenv("SENSE_OPEN_ACCESS", "1") != "0"


def _is_admin() -> bool:
    if (session.get("email") or "").lower() in _ADMIN_EMAILS:
        return True
    key = os.getenv("SENSE_METRICS_KEY")
    return bool(key) and request.args.get("key") == key


# Кого нельзя показывать клиентке как «стилевой ориентир». Реальный баг (кейс Марины, 16.07.2026):
# модель видит имя автора в правилах Tone of Voice («тексты звучат как Ксения Колупаева») и
# подставляет её клиентке как персону-прототип. Автор методологии — это голос текстов, а не ориентир.
# Промпт это уже запрещает, но страхуемся в коде: модель может сдрейфовать снова.
_PROTOTYPE_BANNED = ("колупаева", "ксения колупаева", "kolupaeva", "sense style", "чувство стиля")


def _clean_prototypes(protos: list) -> list:
    """Убрать из прототипов автора/сервис. Пустой список честнее подстановки автора."""
    out = []
    for p in protos:
        name = (p.get("name") if isinstance(p, dict) else str(p)) or ""
        if any(b in name.lower() for b in _PROTOTYPE_BANNED):
            continue
        out.append(p)
    return out


def _stylebook_access(email: str) -> bool:
    """Гейт фото-стайлбука: он входит в платный пакет «Преображение». Пока оплаты (ЮKassa) нет —
    доступ у админа и у профилей с флагом premium (ставит фаундер вручную / позже — после оплаты).
    Список премиум-почт можно задать через env SENSE_PREMIUM_EMAILS (через запятую)."""
    if _is_admin():
        return True
    em = (email or "").lower()
    if em and em in {e.strip().lower() for e in os.getenv("SENSE_PREMIUM_EMAILS", "").split(",") if e.strip()}:
        return True
    return bool((get_profile(email) or {}).get("premium"))


# Лимит бесплатных генераций (защита от слива токенов). Админ — без лимита.
# Бесплатный тариф = диагностика по квизу + ОДНА генерация с образами. Текстовая Карта
# (meta='text') попытку не сжигает — см. count_generations.
FREE_GEN_LIMIT = int(os.getenv("SENSE_FREE_GEN_LIMIT", "1"))
# Второй контур: сколько дорогих генераций допускаем с одного IP в сутки. Первый контур висит на
# cookie, а её чистят — без этого анонимный доступ означал бы безлимитный расход ключа.
# 15, а не 5: жюри конкурса и клиентки могут сидеть за общим NAT/корпоративным адресом, и на пятом
# человеке путь бы оборвался. Сверху всё равно стоит DEMO_DAILY_LIMIT на весь сервис.
IP_GEN_LIMIT = int(os.getenv("SENSE_IP_GEN_LIMIT", "15"))
_GEN_LIMIT_MSG = ("Бесплатная генерация Карты уже использована. "
                  "Твоя Карта сохранена — открой её в разделе «Мой профиль».")
_IP_LIMIT_MSG = ("С этого адреса сегодня уже собрали максимум бесплатных Карт. "
                 "Попробуй завтра или напиши нам.")


def _client_ip() -> str:
    """IP клиентки. За обратным прокси (Amvera) реальный адрес — первый в X-Forwarded-For."""
    fwd = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return fwd or (request.remote_addr or "")


def _gen_allowed(email: str) -> bool:
    """Можно ли этому пользователю запускать дорогую генерацию Карты (бесплатный лимит)."""
    if _is_admin():
        return True
    return count_generations(email) < FREE_GEN_LIMIT


def _ip_gen_allowed() -> bool:
    """Не исчерпан ли суточный лимит генераций с этого IP (страховка от чистки cookie)."""
    if _is_admin():
        return True
    return count_generations_ip(_client_ip()) < IP_GEN_LIMIT


METRICS_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Метрики</title>
<style>body{font-family:Onest,Arial,sans-serif;max-width:820px;margin:0 auto;padding:32px 22px 70px;background:#F5EFE3;color:#1f1d1b}
h1{font-weight:600;font-size:26px} h2{font-size:16px;margin:28px 0 10px;color:#5D2230}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:#fff;border:1px solid #e3dccf;border-radius:12px;padding:14px}
.kpi b{display:block;font-size:28px} .kpi span{color:#6b645c;font-size:13px}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
td,th{text-align:left;padding:8px 6px;border-bottom:1px solid #e3dccf;vertical-align:top}
.star{color:#5D2230}</style></head><body>
<h1>Метрики продукта</h1>

<h2>Почта (вход по ссылке)</h2>
<div style="background:#fff;border:1px solid #e3dccf;border-radius:12px;padding:16px 18px">
{% if email_ok %}
 <p style="margin:0 0 6px">Статус: <b style="color:#3b7a4b">настроена ✓</b> — клиентки получают ссылку входа на почту.</p>
{% else %}
 <p style="margin:0 0 6px">Статус: <b style="color:#9b3030">НЕ настроена ✕</b> — письма не уходят. Нужны ОБЕ переменные ниже.</p>
{% endif %}
 <p style="margin:0 0 6px;font-size:14px">Активный способ отправки: <b>{{ 'SMTP (' + smtp_host + ')' if has_smtp else ('UniSender GO' if has_key and has_from else '— не настроен —') }}</b></p>
 <ul style="margin:0 0 10px;padding-left:18px;font-size:14px">
  <li><b>Способ 1 — SMTP (проще, свой ящик):</b> <code>SMTP_USER</code>: {{ 'задан ✓' if has_smtp_user else '✕' }}, <code>SMTP_PASSWORD</code>: {{ 'задан ✓' if has_smtp_pass else '✕' }} (для Яндекса — пароль приложения; хост по умолч. smtp.yandex.ru:465)</li>
  <li><b>Способ 2 — UniSender GO:</b> <code>UNISENDER_API_KEY</code>: {{ 'задан ✓' if has_key else '✕' }}, <code>UNISENDER_FROM_EMAIL</code>: {{ 'задан ✓' if has_from else '✕' }}, сервер <b>{{ api_host }}</b> (для go2 задай <code>UNISENDER_API_URL</code>)</li>
 </ul>
 <form method=post action="/metrics/test-email" style="margin:0">
  <input type=hidden name=key value="{{ keyq[5:] }}">
  <button type=submit style="font:inherit;font-size:14px;padding:9px 16px;border-radius:8px;cursor:pointer;border:1px solid #5D2230;background:#fff;color:#5D2230">Отправить тестовое письмо себе</button>
  {% if mail_test %}<span style="margin-left:12px;color:{{ '#3b7a4b' if mail_test_ok else '#9b3030' }}">{{ mail_test }}</span>{% endif %}
 </form>
</div>

<h2>Воронка <span style="font-weight:normal;font-size:12px;color:#6b645c">— только живые клиентки</span></h2>
<div class=grid>
 <div class=kpi><b>{{ f.quiz_done }}</b><span>прошли квиз (диагностик)</span></div>
 <div class=kpi><b>{{ f.unique_clients }}</b><span>уникальных клиенток</span></div>
 <div class=kpi><b>{{ f.card_form_view }}</b><span>открыли форму Карты</span></div>
 <div class=kpi><b>{{ f.card_built }}</b><span>собрали Карту</span></div>
 <div class=kpi><b>{{ f.quiz_to_card_pct }}%</b><span>квиз → Карта</span></div>
 <div class=kpi><b>{{ f.looks_generated }}</b><span>прогонов генерации образов</span></div>
</div>
{% if f.excluded_technical %}
<p class=hint style="color:#6b645c;font-size:13px;margin:8px 0 0">
 Из воронки исключено <b>{{ f.excluded_technical }}</b> технических прохождений (smoke-тесты,
 самотесты автора, анонимы). Считаем спрос, а не свою же работу — иначе цифры для жюри врут.
</p>
{% endif %}
<h2>Identity Gap</h2>
<div class=grid>
 <div class=kpi><b>{{ g.clients_measured }}</b><span>замерено клиенток</span></div>
 <div class=kpi><b>{{ g.avg_first_gap if g.avg_first_gap is not none else '—' }}%</b><span>средний Gap (старт)</span></div>
 <div class=kpi><b>{{ g.clients_with_progress }}</b><span>с повторным замером</span></div>
 <div class=kpi><b>{{ g.same_day_repeats }}</b><span>из них повтор в тот же день (шум)</span></div>
 <div class=kpi><b>{{ g.avg_gap_reduction if g.avg_gap_reduction is not none else '—' }}</b><span>среднее снижение Gap, п.п.</span></div>
</div>
<h2>Почты клиенток ({{ leads|length }}) &nbsp;<a href="/metrics/leads.csv{{ keyq }}">скачать все CSV</a> &nbsp;·&nbsp; <a href="/metrics/unisender.csv{{ keyq }}">для UniSender (согласившиеся)</a></h2>
<table><tr><th>Email</th><th>Письма</th><th>Первый раз</th><th>Последний</th><th>Формула</th><th>Цветотип</th><th>Gap</th><th>Отзывов</th></tr>
{% for l in leads %}<tr><td>{{ l.email }}</td><td>{{ '✓' if l.marketing else '' }}</td><td>{{ l.first }}</td><td>{{ l.last }}</td><td>{{ l.formula or '' }}</td><td>{{ l.colortype or '' }}</td><td>{{ l.gap if l.gap is not none else '' }}</td><td>{{ l.feedback or '' }}</td></tr>{% endfor %}
{% if not leads %}<tr><td colspan=8 style="color:#6b645c">Пока нет почт.</td></tr>{% endif %}
</table>
<h2 id=reviews>Отзывы и комментарии ({{ f.feedback }}{% if f.avg_rating %}, средняя {{ f.avg_rating }}★{% endif %}) &nbsp;<a href="/metrics/feedback.csv{{ keyq }}">скачать CSV</a></h2>
<p style="color:#6b645c;font-size:13px">«Показать на сайте» — отзыв появится в блоке на лендинге. Публикуй только с согласия клиентки.</p>
<table><tr><th>Когда</th><th>Клиентка</th><th>Оценка</th><th>Текст</th><th>На сайте</th></tr>
{% for r in fb %}<tr><td>{{ r.ts }}</td><td>{{ r.client }}</td><td class=star>{{ r.rating or '' }}</td><td>{{ r.text or '' }}</td>
<td>{% if r.text %}<form method=post action="/metrics/feedback/approve" style="margin:0">
 <input type=hidden name=id value="{{ r.id }}"><input type=hidden name=approved value="{{ '0' if r.approved else '1' }}"><input type=hidden name=key value="{{ keyq[5:] }}">
 <button type=submit style="font:inherit;font-size:12px;padding:4px 10px;border-radius:6px;cursor:pointer;border:1px solid {{ '#3b7a4b' if r.approved else '#d9d2c7' }};background:{{ '#eef6ee' if r.approved else '#fff' }};color:{{ '#3b7a4b' if r.approved else '#6b645c' }}">{{ 'показан ✓' if r.approved else 'показать на сайте' }}</button>
</form>{% else %}<span style="color:#c4bdb0">нет текста</span>{% endif %}</td></tr>{% endfor %}
{% if not fb %}<tr><td colspan=5 style="color:#6b645c">Пока нет отзывов.</td></tr>{% endif %}
</table>
<h2>Чат «Спросить стилиста» ({{ chat|length }}) &nbsp;<a href="/metrics/chat.csv{{ keyq }}">скачать CSV</a></h2>
<table><tr><th>Когда</th><th>Кто</th><th>Роль</th><th>Сообщение</th></tr>
{% for m in chat %}<tr><td>{{ m.ts }}</td><td>{{ m.client }}</td><td>{{ 'клиент' if m.role=='user' else 'стилист' }}</td><td>{{ m.text }}</td></tr>{% endfor %}
{% if not chat %}<tr><td colspan=4 style="color:#6b645c">Пока нет переписки.</td></tr>{% endif %}
</table>
</body></html>"""


def _csv_response(rows: list, header: list, fname: str) -> Response:
    import csv
    import io as _io
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    data = "﻿" + buf.getvalue()  # BOM — чтобы Excel корректно открыл кириллицу
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/metrics")
def metrics_page():
    if not _is_admin():
        return redirect("/login?next=/metrics")
    key = request.args.get("key")
    keyq = ("?key=" + key) if key else ""
    mt = request.args.get("mail_test")  # результат тестовой отправки (?mail_test=ok|fail)
    from urllib.parse import urlparse
    api_url = os.getenv("UNISENDER_API_URL",
                        "https://go1.unisender.ru/ru/transactional/api/v1/email/send.json")
    return render_template_string(
        METRICS_PAGE, f=funnel(), g=gap_summary(), fb=feedback_list(), leads=leads(),
        chat=chat_log(), keyq=keyq, email_ok=email_configured(),
        has_key=bool(os.getenv("UNISENDER_API_KEY")), has_from=bool(os.getenv("UNISENDER_FROM_EMAIL")),
        has_smtp_user=bool(os.getenv("SMTP_USER")), has_smtp_pass=bool(os.getenv("SMTP_PASSWORD")),
        has_smtp=bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD")),
        smtp_host=os.getenv("SMTP_HOST", "smtp.yandex.ru"),
        api_host=(urlparse(api_url).hostname or api_url),
        mail_test=({"ok": "Отправлено — проверь свою почту.",
                    "fail": "Не удалось. Проверь ключи Unisender, сервер (go1/go2) и подтверждённого отправителя."}.get(mt)),
        mail_test_ok=(mt == "ok"))


@app.post("/metrics/test-email")
def metrics_test_email():
    """Админ: отправить тестовую ссылку входа на свою же почту — проверить настройку почты на проде."""
    if not _is_admin():
        return redirect("/login?next=/metrics")
    email = session.get("email") or ""
    key = request.form.get("key") or ""
    keyq = ("?key=" + key) if key else ""
    if not email:
        return redirect("/metrics" + keyq)
    link = request.url_root.rstrip("/") + "/auth?token=" + make_token(email)
    ok = send_magic_link(email, link)
    sep = "&" if keyq else "?"
    return redirect("/metrics" + keyq + sep + "mail_test=" + ("ok" if ok else "fail"))


@app.get("/api/reviews")
def api_reviews():
    """Публичные ОДОБРЕННЫЕ отзывы для блока на лендинге. Без email (приватность)."""
    return jsonify({"reviews": approved_feedback(limit=12)})


@app.post("/metrics/feedback/approve")
def metrics_feedback_approve():
    """Модерация отзыва (админ): одобрить/снять для публичного показа. Возврат на /metrics."""
    if not _is_admin():
        return redirect("/login?next=/metrics")
    try:
        fid = int(request.form.get("id") or 0)
    except ValueError:
        fid = 0
    if fid:
        set_feedback_approved(fid, request.form.get("approved") == "1")
    key = request.form.get("key") or request.args.get("key")
    return redirect("/metrics" + (("?key=" + key) if key else "") + "#reviews")


@app.get("/metrics/leads.csv")
def metrics_leads_csv():
    if not _is_admin():
        return redirect("/login?next=/metrics")
    rows = [[l["email"], l["first"], l["last"], l["formula"], l["colortype"],
             l["figure"], l["gap"], l["sessions"], l["feedback"]] for l in leads()]
    return _csv_response(rows, ["email", "first_seen", "last_seen", "formula", "colortype",
                                "figure", "gap", "sessions", "feedback_count"], "leads.csv")


@app.get("/metrics/unisender.csv")
def metrics_unisender_csv():
    """Выгрузка только согласившихся на письма — для импорта в UniSender (email + Формула как тег)."""
    if not _is_admin():
        return redirect("/login?next=/metrics")
    rows = [[l["email"], l["formula"] or "", l["colortype"] or ""]
            for l in leads() if l["marketing"]]
    return _csv_response(rows, ["email", "formula", "colortype"], "unisender.csv")


@app.get("/metrics/feedback.csv")
def metrics_feedback_csv():
    if not _is_admin():
        return redirect("/login?next=/metrics")
    rows = [[r["ts"], r["client"], r["rating"], r["text"]] for r in feedback_list(limit=1000)]
    return _csv_response(rows, ["ts", "email", "rating", "text"], "feedback.csv")


@app.get("/metrics/chat.csv")
def metrics_chat_csv():
    if not _is_admin():
        return redirect("/login?next=/metrics")
    rows = [[m["ts"], m["client"], m["role"], m["text"]] for m in chat_log(limit=5000)]
    return _csv_response(rows, ["ts", "email", "role", "text"], "chat.csv")


# карта вердикта/совпадений → русские подписи, цвет и иконка
_VERDICT_RU = {"take": ("Брать", "#3b7a4b", "✓"), "replace": ("Подумай", "#b8860b", "↺"),
               "skip": ("Оставь в магазине", "#9b3030", "✕")}
_PALETTE_RU = {"base": "в базе твоей палитры", "accent": "акцент твоей палитры",
               "neutral": "нейтрально", "taboo": "стоп-цвет", "unclear": "не считывается"}
_LINES_RU = {"works": "в твоих линиях", "risky": "рискованно по линиям",
             "wrong": "против твоих линий"}
_STYLE_RU = {"core": "ядро твоего стиля", "adjacent": "смежно со стилем", "off": "вне стиля"}


def _garment_profile(form) -> dict:
    """Экспресс-профиль из анкеты «Примерочной»: визуальные маркеры, ДНК стиля,
    анти-гардероб. Никакой техники (цветотип/фигуру ИИ читает по фото отдельно)."""
    diag = {
        "silhouette_lines": form.get("silhouette_lines") or None,
        "fit_focus": form.get("fit_focus") or None,
        "impression": form.get("impression") or None,
        "fit_challenges": form.getlist("fit_challenges") or None,
        "style_dna": form.getlist("style_dna") or None,
        "dealbreakers": form.getlist("dealbreakers") or None,
    }
    return {k: v for k, v in diag.items() if v}


def _server_profile_json() -> str:
    """JSON профиля «Примерочной» из аккаунта (если вошёл) — для префилла формы; иначе null."""
    email = _current_user()
    if not email:
        return "null"
    sp = (get_profile(email) or {}).get("style_profile") or None
    return json.dumps(sp, ensure_ascii=False)


@app.get("/garment")
def garment():
    return render_template_string(GARMENT_FORM, error=None, profile_json=_server_profile_json())


@app.post("/garment/check")
def garment_check():
    """«Брать / не брать»: фото вещи + Формула → вердикт (одна vision-проверка, без генерации)."""
    if not _quota_left():
        return render_template_string(GARMENT_FORM, error="Демо-лимит на сегодня исчерпан — загляни завтра."), 429
    if not _consent_ok(request.form):
        return render_template_string(GARMENT_FORM, error="Нужно согласие на обработку и передачу фото."), 400
    record_consent((request.form.get("client") or "").strip() or "anonymous",
                   request.remote_addr or "", True, True)
    try:
        photo_path = _validate_and_save(request.files.get("photo"))
    except ValueError as e:
        return render_template_string(GARMENT_FORM, error=str(e)), 400

    diag = _garment_profile(request.form)
    if session.get("email"):  # вошла — сохраняем анкету в профиль (заполнить один раз)
        save_style_profile(session["email"], diag)
    record_call()
    try:
        v = evaluate_garment(str(photo_path), diag, mode="dev")
    except Exception as e:  # noqa: BLE001
        return render_template_string(GARMENT_FORM, error=f"Не удалось проверить: {e}"), 500

    verdict_ru, color, icon = _VERDICT_RU.get(v.get("verdict"), ("Спорно", "#6b645c", "?"))
    palette = _PALETTE_RU.get(v.get("palette_match"))
    if v.get("palette_match") == "unclear":
        palette = None  # цветотип неизвестен — не показываем «не считывается»
    return render_template_string(
        GARMENT_RESULT, verdict_ru=verdict_ru, color=color, icon=icon,
        item=v.get("item"), reason=v.get("reason", ""),
        replace_with=v.get("replace_with"),
        palette=palette,
        figure=_LINES_RU.get(v.get("lines_match")),
        style=_STYLE_RU.get(v.get("style_match")),
        dealbreaker=v.get("dealbreaker"),
        # Главный аргумент покупки — измеримый: не «подойдёт», а «+N образов к твоей капсуле».
        adds=adds_looks(v.get("item") or "", _current_capsule(email)),
    )


def _validate_and_save(file) -> Path:
    """Проверить и сохранить загруженное фото. ValueError с понятным текстом — если не ок."""
    if not file or not file.filename:
        raise ValueError("Загрузи фото.")
    if file.mimetype not in ALLOWED:
        raise ValueError("Формат не поддержан (нужен JPEG/PNG/WebP).")
    raw = file.read()
    try:  # валидируем, что это реально изображение (защита от мусора/бомб)
        Image.open(io.BytesIO(raw)).verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise ValueError("Файл не похож на изображение.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / secure_filename(file.filename or "photo.jpg")
    path.write_bytes(raw)
    return path


def _build_quiz(form) -> dict:
    return {
        "context": {"age": form.get("age"), "profession": form.get("profession")},
        "now_traits": _split(form.get("now_traits")),
        "want_traits_top3": _split(form.get("want_traits"))[:3],
        "physical": {"height": form.get("height"),
                     "figure_type_self_assessed": form.get("figure")},
        "price_segment": form.get("price", "middle"),
        "taboos": _split(form.get("taboos")),
        "colortype_known": form.get("colortype_known") or None,
    }


def _run_analysis(photo_path: Path, quiz: dict) -> tuple[dict, dict, list]:
    """Сквозной анализ: vision → диагностика → капсула → рендер N образов на клиентке."""
    vision = analyze_photos([str(photo_path)], height_cm=quiz["physical"]["height"], mode="dev")
    if quiz.get("colortype_known"):  # клиентка знает свой цветотип → перебивает ИИ (шаг колориста)
        vision["colortype"] = quiz["colortype_known"]
    diag = diagnose(quiz, vision, mode="dev")
    diag = refine_colortype_subtype(diag, str(photo_path))  # подтип по измеренному контрасту
    gen_req = {"mode": "capsule", "capsule_type": "auto", "season": "FW 2026-2027",
               "scenarios": ["работа", "повседневное", "выход"], "n_looks": 3,
               "price_segment": quiz["price_segment"], "taboos": quiz["taboos"]}
    capsule = generate_capsule(diag, gen_req, mode="dev")
    looks_src = (capsule.get("looks") or [])[:N_RENDER]

    def _render(lk):
        # Сезон капсулы прокидываем в картинку: без него модель одевала клиентку не по погоде.
        return {"img": render_look_on_client(str(photo_path), lk.get("image_generation_prompt", ""),
                                            season="fw"),
                "desc": lk.get("description", "")}

    # образы рендерим ПАРАЛЛЕЛЬНО (одновременно) — это вдвое быстрее последовательного
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(looks_src))) as ex:
        looks = list(ex.map(_render, looks_src))
    return diag, capsule, looks


def _quota_left() -> bool:
    return count_today() < DEMO_DAILY_LIMIT


def _consent_ok(form) -> bool:
    """152-ФЗ: нужно согласие на обработку ПД и на трансграничную передачу фото в AI."""
    return bool(form.get("consent_processing")) and bool(form.get("consent_transfer"))


@app.post("/analyze")
def analyze():
    if not _quota_left():
        return render_template_string(FORM, error="Демо-лимит на сегодня исчерпан — загляни завтра."), 429
    if not _consent_ok(request.form):
        return render_template_string(FORM, error="Нужно согласие на обработку данных и трансграничную передачу."), 400
    record_consent((request.form.get("client") or "").strip() or "anonymous",
                   request.remote_addr or "", True, True)
    try:
        photo_path = _validate_and_save(request.files.get("photo"))
    except ValueError as e:
        return render_template_string(FORM, error=str(e)), 400

    quiz = _build_quiz(request.form)
    record_call()  # фиксируем платный вызов для квоты
    try:
        diag, capsule, looks = _run_analysis(photo_path, quiz)
    except Exception as e:  # noqa: BLE001 — понятная ошибка, не страница 500
        return render_template_string(FORM, error=f"Не удалось обработать: {e}"), 500

    client = (request.form.get("client") or "").strip()
    prog = None
    if client:  # трекинг динамики Identity Gap во времени
        record_session(client, diag)
        prog = progress(client)
    if session.get("email"):  # вошла — сохраняем Формулу в профиль
        save_diagnosis(session["email"], diag)

    cap = capsule.get("capsule") or {}
    return render_template_string(
        RESULT, formula=diag.get("style_formula"), gap=diag.get("gap_percentage"),
        dna=diag.get("dna_explanation", ""), colortype=_colortype_label(diag.get("colortype")),
        figure=_figure_label(diag.get("figure_type")), items=len(cap.get("items") or []), looks=looks,
        prog=prog,
    )


_JOBS: dict = {}  # job_id -> {status: processing|done|error, result|error}
_PENDING_DIR = _data_dir() / "pending_diag"  # диагноз анонимного квиза на диске (переживает рестарт)


def _save_pending_diag(job_id: str, diag: dict) -> None:
    """Диагноз анонимного квиза → на диск. In-memory _JOBS теряется при перезапуске сервера
    (особенно с --debug: рестарт от каждой правки), из-за чего /card?from_job не находил диагноз
    и зацикливал на квиз. Файл это чинит — диагноз доступен после рестарта."""
    try:
        _PENDING_DIR.mkdir(parents=True, exist_ok=True)
        (_PENDING_DIR / f"{job_id}.json").write_text(
            json.dumps(diag, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _load_pending_diag(job_id: str) -> dict | None:
    try:
        f = _PENDING_DIR / f"{job_id}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return None


_FIELD_RU = {"natural": "естественность", "romance": "женственность",
             "drama": "выразительность", "classic": "структура"}
_CONTRAST_RU = {"low": "мягкий", "medium": "средний", "high": "высокий"}

# Названия типов фигуры для показа клиентке — язык ПРОПОРЦИЙ и ЛИНИЙ (Body Liberation,
# международная практика 2026), без «фруктов» и геометрии. Внутренние коды не меняем.
_FIGURE_LABEL = {
    "rectangle": "Прямой силуэт, сбалансированные пропорции",
    "hourglass": "Выраженная талия, сбалансированные пропорции",
    "inverted_triangle": "Выраженная линия плеч, более узкие бёдра",
    "pear": "Объём в бёдрах, выраженная талия",
    "apple": "Мягкие линии, объём в центре, стройные ноги",
}
# Короткое имя фигуры — для чипов и шапок, где длинное описание разносит вёрстку («Выраженная
# талия, сбалансированные пропорции» занимало половину строки профиля). Описание остаётся в
# подсказке и в разборе. Называем геометрией, а не «груша/яблоко»: клиентке не нужен ярлык-овощ.
# Раздел 8 метода («Словарь языка») прямо запрещает показывать клиентке ярлык фигуры:
# не «Тип фигуры: Прямоугольник», а «сбалансированные плечи и бёдра». Геометрия — наш рабочий
# код для подбора, клиентка же читает про свои пропорции. Ярлыки остаются внутри движка.
_FIGURE_SHORT = {
    "rectangle": "Плечи и бёдра в балансе",
    "hourglass": "Выраженная талия",
    "inverted_triangle": "Акцент в плечах",
    "pear": "Акцент в бёдрах",
    "apple": "Мягкая линия талии",
}
_COLORTYPE_LABEL = {
    "spring_light": "Весна светлая", "spring_natural": "Весна натуральная",
    "spring_contrast": "Весна контрастная", "summer_light": "Лето светлое",
    "summer_natural": "Лето натуральное", "summer_contrast": "Лето контрастное",
    "autumn_light": "Осень светлая", "autumn_natural": "Осень натуральная",
    "autumn_contrast": "Осень контрастная", "winter_light": "Зима светлая",
    "winter_natural": "Зима натуральная", "winter_contrast": "Зима контрастная",
}


# карточки стилей для визуального выбора в анкете (изображения web/photos/styles/<code>.png)
_STYLE_CARDS = {
    "classic": {"label": "Классика", "hint": "структура, качество, сдержанность"},
    "drama": {"label": "Драма", "hint": "контраст, характер, заметность"},
    "romantic": {"label": "Романтика", "hint": "мягкость, женственность, изящество"},
    "natural": {"label": "Натуральный", "hint": "свобода, комфорт, естественность"},
}
_STYLE_LABEL = {c: v["label"] for c, v in _STYLE_CARDS.items()}


def _figure_label(code):
    return _FIGURE_LABEL.get(code, code) if code else code


# Четыре поля метода — то, из чего складывается ДНК стиля. Порядок фиксирован, чтобы полоса
# не «прыгала» между сборками, а цвета взяты из бренд-палитры.
_FIELD_ORDER = ("classic", "drama", "romance", "natural")
_FIELD_LABEL = {"classic": "Классика", "drama": "Драма",
                "romance": "Романтика", "natural": "Натуральный"}
_FIELD_COLOR = {"classic": "#5D2230", "drama": "#8A3346",
                "romance": "#C08A9B", "natural": "#A99684"}


def _dna_fields(diag: dict) -> list[dict]:
    """ДНК стиля в долях: [{code, label, pct, hex}] по убыванию. Пусто — если диагностика старая.

    Это не украшение, а результат теста: формула называет направление, а доли показывают, из чего
    оно собрано. Нормируем к 100 — модель иногда отдаёт сумму 98 или 103.
    """
    dist = diag.get("semantic_field_distribution") or {}
    vals = {k: float(dist.get(k) or 0) for k in _FIELD_ORDER}
    total = sum(vals.values())
    if total <= 0:
        return []
    out = [{"code": k, "label": _FIELD_LABEL[k], "hex": _FIELD_COLOR[k],
            "pct": round(v * 100 / total)} for k, v in vals.items() if v > 0]
    return sorted(out, key=lambda f: -f["pct"])


def _figure_short(code):
    """Короткое имя фигуры для чипов. Нет в словаре — отдаём длинное, но обрезанное по первой
    запятой: лучше «Прямой силуэт», чем строка на всю ширину экрана."""
    if not code:
        return code
    short = _FIGURE_SHORT.get(code)
    if short:
        return short
    return str(_FIGURE_LABEL.get(code, code)).split(",")[0]


# капсула по одежде: раскладываем вещи по слотам гардероба для наглядного борда
_CAPSULE_SLOTS = [
    ("Верхний слой", ("пальто", "тренч", "жакет", "пиджак", "куртка", "косуха", "кардиган",
                       "плащ", "шуба", "дублёнка", "дубленка", "бомбер", "джинсовк", "труакар",
                       "пуховик", "жилет")),
    ("Платья и комбинезоны", ("платье", "комбинезон", "сарафан")),
    ("Верх", ("рубашка", "блуз", "топ", "футболк", "водолазк", "свитер", "джемпер", "худи",
              # «=поло» — точное слово: иначе «поло» ловит «в полоску» и брюки уезжают в Верх
              "свитшот", "боди", "лонгслив", "майка", "=поло", "тельняшк", "корсет", "бюстье", "кроп")),
    ("Низ", ("брюки", "джинс", "юбка", "шорты", "палаццо", "легинс", "чинос", "кюлот")),
    # WB кладёт обувь в общую категорию «одежда» — слот вытягиваем из имени, поэтому список
    # должен знать и разговорные названия (ботфорты, сабо, дутики), иначе вещь падает в «Прочее».
    ("Обувь", ("туфли", "лодочки", "ботинки", "ботильон", "челси", "сапог", "кроссовк", "кед",
               "босоножк", "лофер", "балетк", "сандал", "мюли", "слипон", "угги", "ботфорт",
               "мокасин", "сабо", "дутик", "шлепанц", "сланц", "эспадрил", "броги", "оксфорд")),
    ("Аксессуары", ("сумк", "ремень", "пояс", "шарф", "платок", "косынк", "очки", "шляп", "берет",
                    "кепк", "серьг", "браслет", "колье", "цепочк", "часы", "перчатк", "клатч",
                    "шопер", "аксессуар")),
]

_SLOT_OTHER = "Прочее"


def _capsule_slot(*names: str) -> str:
    """Слот вещи по первому распознанному признаку. Пробуем по очереди все переданные строки
    (категория, затем имя): категории фида часто общие — «одежда», «комплект», «трикотаж» — и
    ничего не говорят о слоте, тогда решает имя («Топ из вискозы» → Верх, «Косынка» → Аксессуары).
    Раньше имя бралось только при ПУСТОЙ категории, и половина каталога падала в мусорный слот."""
    for raw in names:
        n = (raw or "").lower()
        if not n:
            continue
        # Побеждает ключ, стоящий РАНЬШЕ в названии, а не раньше в списке слотов. Вещь называют по
        # главному предмету, детали идут следом: «Пальто свободное демисезонное с поясом» — это
        # пальто, а не пояс, но при переборе по порядку слотов оно уезжало в «Аксессуары».
        best: tuple[int, str] | None = None
        for slot, keys in _CAPSULE_SLOTS:
            for k in keys:
                # ключ с «=» — только целым словом. Остальные ключи намеренно усечены под
                # морфологию («блуз» ловит блузу и блузку), но короткие вроде «поло» так
                # попадают внутрь чужих слов («в полоску») и утаскивают вещь в чужой слот.
                if k.startswith("="):
                    hit = re.search(rf"\b{re.escape(k[1:])}\b", n)
                    pos = hit.start() if hit else -1
                else:
                    pos = n.find(k)
                if pos >= 0 and (best is None or pos < best[0]):
                    best = (pos, slot)
        if best:
            return best[1]
    return _SLOT_OTHER


# Порядок ячеек конструктора. Отличается от _CAPSULE_SLOTS сознательно: тот словарь задаёт
# порядок ХРАНЕНИЯ вещей, а здесь человек СОБИРАЕТ образ и думает иначе — сначала основа
# (верх и низ либо платье), потом всё, что её завершает. Раньше первой ячейкой шёл верхний
# слой, и конструктор начинался с пальто, когда под ним ещё ничего нет.
_OUTFIT_CELL_GROUPS = [
    ("Основа образа", ["Верх", "Низ", "Платья и комбинезоны"]),
    ("Завершение", ["Верхний слой", "Обувь", "Аксессуары"]),
]


def _outfit_cells(board: list[dict]) -> list[dict]:
    """Ячейки конструктора: только те слоты, что есть в капсуле, в порядке сборки образа."""
    have = {grp.get("slot") for grp in board or []}
    out = []
    for title, slots in _OUTFIT_CELL_GROUPS:
        cells = [s for s in slots if s in have]
        if cells:
            out.append({"title": title, "slots": cells})
    return out


def _merge_boards(primary: list, extra: list, limit: int) -> list:
    """Борд из капсулы Карты, добранный вещами каталога до limit.

    Вещи Карты идут первыми и не вытесняются: это опора, которую клиентка уже видела в образах.
    Каталог только заполняет пустые слоты, чтобы в конструкторе было из чего собирать.
    """
    by_slot: dict[str, list] = {}
    seen: set[str] = set()
    total = 0
    for board in (primary, extra or []):
        for grp in board or []:
            slot = grp.get("slot") or ""
            for it in grp.get("items") or []:
                name = " ".join((it.get("name") or "").lower().split())
                if not name or name in seen:
                    continue
                # Конструктор — визуальный инструмент: вещь без картинки в нём бесполезна,
                # её нельзя перетащить в образ и увидеть результат. Раньше такие вещи давали
                # ряд пустых бежевых плиток, и половина капсулы выглядела недогруженной.
                if not it.get("image"):
                    continue
                if total >= limit:
                    break
                seen.add(name)
                by_slot.setdefault(slot, []).append(it)
                total += 1
    order = [s for s, _ in _CAPSULE_SLOTS] + [_SLOT_OTHER]
    return [{"slot": s, "items": by_slot[s]} for s in order if by_slot.get(s)]


def _capsule_board(items: list) -> list:
    """Группировка вещей капсулы по слотам гардероба (для визуального борда). Порядок — как в _CAPSULE_SLOTS."""
    order = [s for s, _ in _CAPSULE_SLOTS] + [_SLOT_OTHER]
    groups: dict[str, list] = {}
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            groups.setdefault(_capsule_slot(it["name"]), []).append(it)
    return [{"slot": s, "items": groups[s]} for s in order if groups.get(s)]


def _colortype_label(code):
    return _COLORTYPE_LABEL.get(code, code) if code else code


def _explainability(diag: dict, quiz: dict) -> dict:
    """Блоки «что AI увидел по фото» и «почему AI так решил» — из полей диагностики,
    без дополнительных LLM-вызовов. Объяснимость (Explainable AI) для жюри."""
    dist = diag.get("semantic_field_distribution") or {}
    top = [k for k, _ in sorted(dist.items(), key=lambda kv: kv[1], reverse=True)
           if dist.get(k, 0) > 0][:2]
    tonal = diag.get("tonal_characteristics") or {}
    vf = diag.get("visual_formula") or {}
    want = quiz.get("want_traits_top3") or []
    now = quiz.get("now_traits") or []

    signals = []
    if want:
        signals.append("Ты хочешь, чтобы тебя считывали как " + ", ".join(want[:3]) + ".")
    if top:
        signals.append("В ответах сильнее всего звучат " + " и ".join(
            _FIELD_RU.get(t, t) for t in top) + ".")
    if now:
        signals.append("Сейчас считывают как " + ", ".join(now[:3])
                       + " — это и есть разрыв, который закрываем.")

    contrast = _CONTRAST_RU.get(tonal.get("contrast") or "", tonal.get("contrast") or "")
    sil = (vf.get("silhouettes") or [None])[0]
    risk = ("Текущий образ может считываться спокойнее и проще, чем желаемый"
            if now and want else None)
    photo = {
        "contrast": contrast,
        "silhouette": sil,
        "colortype": _colortype_label(diag.get("colortype")),
        "figure": _figure_label(diag.get("figure_type")),
        "risk": risk,
    }
    return {
        "signals": signals,
        "photo": photo,
        "dna": diag.get("dna_explanation", ""),
        "stop_list": (vf.get("stop_list") or [])[:4],
        "rules": diag.get("retrieved_rules") or [],  # RAG: сработавшие правила базы
    }


def _run_fast(photo_path: Path, quiz: dict, season: str | None = None):
    """Быстрый путь для квиза: vision → диагностика → 2 именованных направления →
    рендер параллельно. Возвращает (diag, directions, explain).

    directions[i] = {name, fits_if, items[], img}. explain — блоки объяснимости.
    season — spring|summer|autumn|winter: образы собираются под сезон.
    """
    vision = analyze_photos([str(photo_path)], height_cm=quiz["physical"]["height"], mode="dev")
    if quiz.get("colortype_known"):
        vision["colortype"] = quiz["colortype_known"]
    diag = diagnose(quiz, vision, mode="dev")
    diag = refine_colortype_subtype(diag, str(photo_path))  # подтип по измеренному контрасту
    directions = generate_directions(diag, quiz, season=season, mode="dev")[:N_RENDER]
    if not directions:  # генерация направлений не сработала — синтезируем из диагностики
        directions = _fallback_directions(diag)

    def _render(d):
        return {
            "name": d.get("name", ""),
            "fits_if": d.get("fits_if", ""),
            # Куда надеть — главное, что клиентка хочет знать про образ. Список вещей отвечает
            # на «как повторить», а поводы — на «зачем мне это».
            "wear_to": [w for w in (d.get("wear_to") or []) if w][:3],
            "items": d.get("items") or [],
            "img": render_look_on_client(str(photo_path), _look_prompt(d, diag, season),
                                         season=season),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(directions))) as ex:
        rendered = list(ex.map(_render, directions))
    return diag, rendered, _explainability(diag, quiz)


def _palette_names(diag: dict) -> str:
    pal = (diag.get("visual_formula") or {}).get("palette") or []
    names = [p.get("name", "") for p in pal if p.get("role") in ("base", "accent")][:4]
    return ", ".join(n for n in names if n)


_SEASON_LOOK = {
    "spring": "сезон: весна, лёгкие слои (тренч/рубашка), светлые ткани",
    "summer": "сезон: лето, лёгкие ткани (лён/хлопок/шёлк), без верхней одежды, открытая обувь",
    "autumn": "сезон: осень, многослойность, трикотаж и жакет/пальто, плотные ткани, ботинки",
    "winter": "сезон: зима, тёплый слой (пальто/шерсть/кашемир), закрытый силуэт, сапоги",
}


def _look_prompt(d: dict, diag: dict, season: str | None = None) -> str:
    """Промпт образа для рендера. Строим из РЕАЛЬНЫХ вещей направления + палитры +
    силуэта — чтобы он был конкретным, отличался между направлениями и НИКОГДА не был
    пустым (иначе identity-рендер просто повторяет исходную одежду)."""
    items = ", ".join(d.get("items") or [])
    pal = _palette_names(diag)
    figure = diag.get("figure_type") or ""
    base = (d.get("image_generation_prompt") or "").strip()
    parts = []
    if d.get("name"):
        parts.append(f"Образ «{d['name']}»")
    if items:
        parts.append(f"вещи: {items}")
    if pal:
        parts.append(f"палитра: {pal}")
    if figure:
        parts.append(f"силуэт под фигуру {figure}")
    if season in _SEASON_LOOK:
        parts.append(_SEASON_LOOK[season])
    if base:
        parts.append(base)
    prompt = ". ".join(parts)
    return prompt or (diag.get("style_formula") or "современный стильный образ в нейтральной палитре")


def _fallback_directions(diag: dict) -> list[dict]:
    """Если generate_directions не дала результата — 2 направления из visual_formula,
    чтобы демо всё равно показало образы (а не исходное фото без изменений)."""
    sils = (diag.get("visual_formula") or {}).get("silhouettes") or []
    formula = diag.get("style_formula") or "твоя Формула стиля"
    return [
        {"name": "Мягкая версия", "fits_if": "Подходит, если хочется спокойствия и уместности.",
         # Поводы и в фолбэке: без них карточка теряет главное — куда этот образ надеть.
         "wear_to": ["встреча с друзьями", "рабочий день без дресс-кода", "прогулка по городу"],
         "items": sils[:3] or ["мягкий жакет", "прямые брюки", "блуза"],
         "image_generation_prompt": f"Спокойный образ по формуле «{formula}», мягкие чистые линии."},
        {"name": "Собранная версия", "fits_if": "Подходит, если хочется уверенности и статуса.",
         "wear_to": ["переговоры", "презентация проекта", "деловой ужин"],
         "items": sils[1:4] or ["структурный жакет", "юбка-карандаш", "рубашка"],
         "image_generation_prompt": f"Собранный образ по формуле «{formula}», структура и один акцент."},
    ]


def _job_worker(job_id: str, photo_path: Path, quiz: dict, client: str,
                account_email: str | None = None, season: str | None = None) -> None:
    """Фоновая генерация — чтобы HTTP-запрос не висел (таймауты/блокировка)."""
    try:
        diag, directions, explain = _run_fast(photo_path, quiz, season=season)
        _save_pending_diag(job_id, diag)  # на диск: /card?from_job найдёт диагноз даже после рестарта
        if client:
            try:
                record_session(client, diag)
            except Exception:  # noqa: BLE001
                pass
        if account_email:  # вошла — сохраняем Формулу в профиль
            try:
                save_diagnosis(account_email, diag)
            except Exception:  # noqa: BLE001
                pass
        _JOBS[job_id] = {"status": "done", "diag": diag, "result": {
            "gap_percentage": diag.get("gap_percentage"),
            "style_formula": diag.get("style_formula"),
            "colortype": diag.get("colortype"),
            "figure_type": diag.get("figure_type"),
            "directions": directions,
            "explain": explain,
            # совместимость со старым фронтом: образы из направлений
            "looks": [{"img": d["img"], "desc": d.get("name", "")} for d in directions],
        }}
    except Exception as e:  # noqa: BLE001
        _JOBS[job_id] = {"status": "error", "error": str(e)}


@app.post("/api/analyze")
def api_analyze():
    """Старт асинхронной генерации. Возвращает job_id; результат — через /api/result/<id>."""
    if not _quota_left():
        return jsonify({"error": "daily_limit"}), 429
    if not _consent_ok(request.form):
        return jsonify({"error": "consent_required"}), 400
    record_consent((request.form.get("client") or "").strip() or "anonymous",
                   request.remote_addr or "", True, True)
    try:
        photo_path = _validate_and_save(request.files.get("photo"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    quiz = _build_quiz(request.form)
    record_call()
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "processing"}
    # запоминаем последний квиз в сессии: тарифные кнопки ведут на /card без ?from_job=, и без этого
    # анонимный диагноз (под job_id, не под почтой) терялся — /card кидал обратно на квиз по кругу
    session["last_job"] = job_id
    client = (request.form.get("client") or "").strip()
    account_email = session.get("email")
    season = (request.form.get("season") or "").strip() or None
    threading.Thread(target=_job_worker,
                     args=(job_id, photo_path, quiz, client, account_email, season),
                     daemon=True).start()
    return jsonify({"job_id": job_id}), 202


_DIRECTION_RU = {"classic": "Классика", "drama": "Драма",
                 "romance": "Романтика", "natural": "Натуральность"}


def _quiz_only_diag(quiz: dict, gap_hint, direction_hint) -> dict:
    """Диагноз из одних ответов квиза, без модели.

    Страховка на случай, когда provider недоступен (кончился ключ, лимит, сеть). Без неё
    клиентка, пропустившая фото, теряет диагностику и на /card упирается в «сначала диагностика» —
    то есть проходит квиз впустую. Лучше отдать честный диагноз по ответам, чем развернуть её.
    """
    code = direction_hint if direction_hint in _DIRECTION_RU else "classic"
    gap = gap_hint if isinstance(gap_hint, int) and 0 <= gap_hint <= 100 else 50
    # base_style читает каталог (_FORMULA_CATEGORIES) и ждёт КОД, не русское название: с «Классика»
    # словарь категорий молча возвращал пустой список, фильтр по формуле отключался — и в капсулу
    # «Классики» приходили кружевные накидки и юбки с воланами. Русское имя живёт в style_formula.
    return {
        "style_formula": _DIRECTION_RU[code],
        "base_style": code,
        "style_dominant": code,
        # Доминанта нужна _visual_capsule: без распределения список styles пуст и стилевого
        # совпадения в скоринге не происходит вовсе.
        "semantic_field_distribution": {
            k: (60 if k == code else 40 // 3) for k in _DIRECTION_RU
        },
        "gap_percentage": gap,
        "now_traits": quiz.get("now_traits") or [],
        "want_traits_top3": quiz.get("want_traits_top3") or [],
        "quiz_only": True,  # фото не было: цветотип и фигуру уточняем позже, при сборке Карты
    }


@app.post("/api/quiz-diagnosis")
def api_quiz_diagnosis():
    """Диагностика по ответам квиза, когда клиентка пропустила фото.

    Раньше job_id заводился только в /api/analyze, то есть только вместе с фото. Кто нажимал
    «Пропустить · показать результат без фото», уходил на /card без диагноза — и продукт
    разворачивал его на «Сначала — диагностика», хотя квиз был пройден целиком.
    """
    data = request.get_json(silent=True) or {}
    quiz = {
        "context": {},
        "now_traits": _split(data.get("now_traits")),
        "want_traits_top3": _split(data.get("want_traits"))[:3],
        "physical": {"height": None, "figure_type_self_assessed": None},
        "price_segment": "middle",
        "taboos": [],
        "colortype_known": None,
    }
    # Модель здесь НЕ зовём, сознательно. Без фото у diagnose нет входных данных для честного
    # разрыва — на проде он выдавал вырожденные 99%, тогда как клиентка только что увидела в
    # квизе 31%. Плюс синхронный вызов занимал ~20 секунд, и кнопка «Получить Карту» всё это
    # время была мёртвой. Разрыв считает квиз, направление квиз тоже определяет сам (3 главные
    # характеристики → поле по методу), а подстиль уточняется при сборке Карты, где есть фото.
    diag = _quiz_only_diag(quiz, data.get("gap"), data.get("direction"))

    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "done", "diag": diag}
    _save_pending_diag(job_id, diag)
    session["last_job"] = job_id  # чтобы /card нашёл диагноз и без ?from_job=
    return jsonify({"job_id": job_id, "gap": diag.get("gap_percentage")})


@app.get("/api/result/<job_id>")
def api_result(job_id):
    """Статус/результат фоновой генерации (без внутреннего diag — он только на сервере)."""
    j = _JOBS.get(job_id) or {"status": "unknown"}
    return jsonify({k: v for k, v in j.items() if k != "diag"})


@app.get("/favicon.ico")
def favicon():
    # Браузер просит /favicon.ico на каждой странице. Без него — 404 в консоли и пустая вкладка.
    # Отдаём SVG-монограмму бренда: файл заводить не нужно, вкладка перестаёт быть безымянной.
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
           "<rect width='64' height='64' rx='14' fill='#5D2230'/>"
           "<text x='32' y='44' text-anchor='middle' font-family='Georgia,serif' "
           "font-size='36' fill='#F5EFE3'>S</text></svg>")
    return app.response_class(svg, mimetype="image/svg+xml",
                              headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz")
def healthz():
    # Флаги настройки внешних сервисов — чтобы проверять прод, не проходя весь путь до кабинета.
    # Секретов не раскрываем: только «задан / не задан».
    # Кеш образов: сколько кадров лежит и сколько генераций он уже сэкономил. Это и метрика
    # экономии ключа для защиты, и способ увидеть, что кеш вообще работает на проде.
    from core import imgcache
    cache = imgcache.stats()
    return {"status": "ok", "calls_today": count_today(), "limit": DEMO_DAILY_LIMIT,
            "img_cache": {**cache, "saved_calls": imgcache.HITS["n"]},
            "weather": weather_configured(), "email": email_configured()}


if __name__ == "__main__":
    # порт 80 — этого ждёт Amvera; в проде запускает gunicorn (см. amvera.yml), не эту строку
    app.run(host="0.0.0.0", port=80, debug=False)
