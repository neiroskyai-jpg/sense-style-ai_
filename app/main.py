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
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, session, send_from_directory)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, evaluate_garment,
                           generate_capsule, generate_card_palette,
                           generate_directions, generate_personality_portrait,
                           generate_shopping_list, generate_styling_pair,
                           refine_colortype_subtype, refine_substyle,
                           render_look_on_client)
from core.tracking import (approved_feedback, chat_log, count_generations, count_today,
                           feedback_list, funnel, gap_progress, gap_summary, leads, progress,
                           record_call, record_chat, record_consent, record_event, record_feedback,
                           record_session, set_feedback_approved)
from core.auth import email_configured, make_token, read_token, send_magic_link
from core.figure_rules import fit_rules_client
from core.chat import stylist_reply
from core.catalog import match_products, parse_csv
from core.profiles import (current_card_by_season, get_profile, save_card,
                           save_diagnosis, save_style_profile)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # дизайнерский сайт (статика)
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим (контроль стоимости/времени)
DEMO_DAILY_LIMIT = int(os.getenv("DEMO_DAILY_LIMIT", "40"))  # защита от слива ключа

# статика сайта раздаётся из web/ в корне; зарегистрированные роуты (/demo, /api…) важнее
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # лимит загрузки 15 МБ
# секрет сессий/magic-link: env SENSE_SECRET_KEY или стабильный файл на постоянном томе
from core.config import secret_key as _secret_key, data_dir as _data_dir  # noqa: E402
app.secret_key = _secret_key()


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

FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sense Style AI</title>
<style>
 :root{--cream:#F5F1EA;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Georgia,serif;max-width:640px;margin:0 auto;padding:28px 20px 70px;background:var(--cream);color:var(--ink);line-height:1.55}
 .top{display:flex;justify-content:space-between;align-items:center}
 .logo{font-size:18px;letter-spacing:.5px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-weight:normal;font-size:30px;margin:14px 0 4px}
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
 <p class=hint>Обработка занимает ~1–2 минуты: анализ фото, диагностика, генерация образов.</p>
</form>
<p class=hint style="margin-top:24px;border-top:1px solid #e3dccf;padding-top:18px">Уже знаешь свою Формулу и стоишь в магазине? <a href="/garment" style="color:#5D2230">Проверь вещь по фото: брать или не брать →</a></p>
</body></html>"""

RESULT = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Твоя Формула стиля</title>
<style>
 :root{--cream:#F5F1EA;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Georgia,serif;max-width:920px;margin:0 auto;padding:28px 20px 70px;background:var(--cream);color:var(--ink);line-height:1.55}
 h1,h2{font-weight:normal} .gap{font-size:42px;color:var(--wine)} .formula{font-size:22px}
 .looks{display:flex;gap:18px;flex-wrap:wrap;margin-top:18px}
 .look{flex:1 1 260px} .look img{width:100%;border-radius:8px} .desc{font-size:14px;color:#444}
 .meta{color:var(--muted);font-size:14px} a{color:var(--wine)}
</style></head><body>
<p><a href="/demo">← заново</a> · <a href="/">на главную</a></p>
<h1>Твоя Формула стиля</h1>
<p class=formula><b>{{ formula }}</b></p>
<p>Identity Gap: <span class=gap>{{ gap }}%</span> — разрыв между тем, как тебя считывают сейчас, и тем, как ты хочешь.</p>
{% if prog and prog.sessions > 1 and prog.delta is not none %}
<p class=meta>Динамика имиджа: было {{ prog.first_gap }}% → стало {{ prog.last_gap }}% ({{ '−' ~ prog.delta if prog.delta >= 0 else '+' ~ (-prog.delta) }} п.п. за {{ prog.sessions }} сессии).</p>
{% endif %}
<p>{{ dna }}</p>
<p class=meta>Цветотип: {{ colortype }} · Фигура: {{ figure }} · В капсуле {{ items }} вещей.</p>
<h2>Ты в новых образах</h2>
<p class=meta>Это короткое превью — 2 образа. Полная Карта стиля собирает палитру из 30 цветов, силуэты, стоп-лист и 6 образов под твои сценарии, с PDF к шкафу.</p>
<div class=looks>
 {% for lk in looks %}
 <div class=look>{% if lk.img %}<img src="{{ lk.img }}" alt="Образ">{% endif %}<p class=desc>{{ lk.desc }}</p></div>
 {% endfor %}
</div>
<div style="margin-top:34px;padding-top:22px;border-top:1px solid #d9d2c7">
 <h2 style="margin:0 0 12px">Что дальше</h2>
 <a href="/card" style="display:inline-block;background:var(--wine);color:#fff;text-decoration:none;padding:14px 26px;border-radius:8px;font-size:16px;margin:0 10px 10px 0">Собрать полную Карту стиля →</a>
 <a href="/garment" style="display:inline-block;background:#fff;color:var(--wine);border:1px solid var(--wine);text-decoration:none;padding:14px 26px;border-radius:8px;font-size:16px;margin:0 10px 10px 0">Проверить вещь по фото</a>
 <a href="/me" style="display:inline-block;color:var(--muted);text-decoration:none;padding:14px 0;font-size:15px">Мой профиль</a>
</div></body></html>"""


LANDING = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Sense Style — стиль, в котором ты настоящая</title>
<style>
 :root{--cream:#F5F1EA;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 *{box-sizing:border-box} body{margin:0;font-family:Georgia,serif;background:var(--cream);color:var(--ink);line-height:1.6}
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
  <p>Персональный стилист на основе психологии моды. Загрузи фото и ответь на несколько вопросов — определим твою Формулу стиля, измерим разрыв между тем, как тебя считывают сейчас и как ты хочешь, и покажем тебя в новых образах.</p>
  <a class=btn href="/demo">Построить свои образы →</a>
 </section>

 <h2>Как это работает</h2>
 <div class=flow>
  <div>1. Фото + короткий квиз</div>
  <div>2. <b>Identity Gap, %</b> — разрыв между «как считывают» и «как хочешь»</div>
  <div>3. Твоя <b>Формула стиля</b> по авторской методологии</div>
  <div>4. Капсула и образы — <b>на тебе</b>, с твоим лицом и фигурой</div>
  <div>5. Список покупок под бюджет и стоп-лист «не покупать»</div>
  <div>6. Трекер: как Identity Gap закрывается со временем</div>
 </div>

 <h2>Что нас отличает</h2>
 <div class=cols>
  <div class=card><h3>Психология, не мода</h3><p>Образ работает на закрытие твоего разрыва идентичности, а не на тренд.</p></div>
  <div class=card><h3>Измеримый результат</h3><p>Identity Gap в % — видно, как меняется впечатление о тебе.</p></div>
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
<style>body{font-family:Georgia,serif;max-width:820px;margin:0 auto;padding:40px 22px 80px;color:#2b2b2b;line-height:1.6}
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
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box}
 body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:600px;margin:0 auto;padding:22px 20px 70px}
 .top{display:flex;justify-content:space-between;align-items:center}
 .logo{font-size:18px;letter-spacing:.5px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .eyebrow{font-family:Arial,sans-serif;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--wine);margin:30px 0 10px}
 h1{font-weight:normal;font-size:34px;line-height:1.12;margin:0 0 12px}
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
 .chip{display:inline-flex;cursor:pointer} .chip input{position:absolute;opacity:0;pointer-events:none}
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
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box}
 body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
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
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>

<div class=vcard style="background:{{ color }}">
 <div class=vicon>{{ icon }}</div>
 <div class=vlabel>вердикт</div>
 <div class=vword>{{ verdict_ru }}</div>
</div>

{% if item %}<p class=item>На фото: {{ item }}</p>{% endif %}
<div class=chips>
 {% if figure %}<span class=chip><span>линии:</span> {{ figure }}</span>{% endif %}
 {% if style %}<span class=chip><span>стиль:</span> {{ style }}</span>{% endif %}
 {% if palette %}<span class=chip><span>цвет:</span> {{ palette }}</span>{% endif %}
</div>
{% if dealbreaker %}<div class=db>⚠ Сработал твой анти-гардероб: {{ dealbreaker }}</div>{% endif %}
<p class=reason>{{ reason }}</p>
{% if replace_with %}<div class=replace><b>Что проверить / чем заменить:</b> {{ replace_with }}</div>{% endif %}

<a class=cta href="/garment">Проверить ещё вещь</a>
<div class=back><a href="/demo">Пройти полную диагностику стиля →</a></div>
</div></body></html>"""


LOGIN_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Вход — Чувство стиля</title>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:460px;margin:0 auto;padding:40px 22px 70px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-size:18px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-weight:normal;font-size:30px;margin:30px 0 8px} .lead{color:var(--muted);margin:0 0 22px}
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
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:560px;margin:0 auto;padding:34px 22px 70px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-size:18px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 h1{font-weight:normal;font-size:30px;margin:26px 0 4px} .email{color:var(--muted);margin:0 0 22px}
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
<div class=top><span class=logo>Чувство стиля</span><a href="/logout">Выйти</a></div>
<h1>Мой профиль</h1>
<p class=email>{{ email }}</p>
<div class=card><h3>Формула стиля {% if has_diag %}<span class="badge yes">есть</span>{% else %}<span class="badge no">ещё нет</span>{% endif %}</h3>
 <p>{% if has_diag %}{{ formula }}{% else %}Пройди диагностику — Формула сохранится здесь.{% endif %}</p></div>
{% if track %}
<div class=card>
 <div class=trackhead><h3>Эволюция <span class=sub>стилевого разрыва</span></h3>
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
 <p class=tnote>Это твоя точка отсчёта. Сделай пере-замер через время — увидишь, как разрыв закрывается. Он двигается только от настоящего замера: новых фото того, как ты одеваешься сейчас.</p>
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
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;font-weight:300;margin:0;background:var(--cream);color:var(--ink);line-height:1.62}
 .wrap{max-width:820px;margin:0 auto;padding:40px 30px 90px}
 .bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
 .bar a,.bar button{color:var(--wine);font:inherit;font-size:14px;background:none;border:0;cursor:pointer;text-decoration:none}
 .stale{background:#fbeee4;border:1px solid #e3cdb8;border-radius:12px;padding:14px 16px;margin:6px 0 20px;font-size:14.5px;color:#5a4a3a;line-height:1.5}
 .stale b{color:var(--wine)} .stale a{display:inline-block;margin-top:9px;background:var(--wine);color:#fff;text-decoration:none;padding:9px 18px;border-radius:8px;font-size:14px}
 .eyebrow{font-family:Arial,sans-serif;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--wine)}
 h1{font-family:'Cormorant Garamond',Georgia,serif;font-weight:600;font-size:54px;line-height:1.04;margin:8px 0 4px;letter-spacing:-.01em} .who{color:var(--muted);margin:0 0 6px}
 h2{font-family:'Cormorant Garamond',Georgia,serif;font-weight:600;font-size:30px;margin:46px 0 16px;border-bottom:1px solid var(--line);padding-bottom:8px;letter-spacing:-.01em}
 .formula{font-family:'Cormorant Garamond',Georgia,serif;font-size:31px;font-weight:600;line-height:1.15;margin:4px 0} .gap{color:var(--wine);font-weight:600}
 .dna{font-size:18px;line-height:1.7;color:#3a352e}
 .sw-group{font-size:13px;color:var(--muted);margin:14px 0 6px;letter-spacing:.04em;text-transform:uppercase}
 .swatches{display:flex;flex-wrap:wrap;gap:10px}
 .sw{width:84px} .sw .chip{height:54px;border-radius:8px;border:1px solid rgba(0,0,0,.08)}
 .sw .nm{font-size:11.5px;color:#4a443c;margin-top:4px;line-height:1.25}
 .stopcolors .chip{height:40px;border-radius:8px} .stopcolors .nm b{color:#9b3030}
 ul.clean{list-style:none;padding:0;margin:0} ul.clean li{padding:6px 0 6px 18px;position:relative;font-size:15.5px}
 ul.clean li:before{content:'·';position:absolute;left:4px;color:var(--wine)}
 .stop li:before{content:'✕';font-size:11px;color:#c07a6a}
 .looks{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 @media(max-width:560px){.looks{grid-template-columns:1fr}}
 .look{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px}
 .look .scn{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:var(--wine)}
 .look .nm{font-family:'Cormorant Garamond',Georgia,serif;font-weight:600;font-size:23px;margin:4px 0 8px;line-height:1.1}
 .look .it{font-size:13.5px;color:#4a443c;margin:0 0 8px}
 .look .ds{font-size:14px;color:#5a5246}
 .cap-h{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine);margin:18px 0 8px;font-weight:normal}
 .blocklead{font-family:Arial,sans-serif;font-size:12px;letter-spacing:.2em;text-transform:uppercase;color:var(--wine);margin:46px 0 -4px;padding-top:22px;border-top:2px solid var(--wine)}
 .blocklead b{opacity:.5;font-weight:normal;margin-right:8px}
 .meta{font-size:15px;color:var(--muted);margin:0 0 10px}
 .caps{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
 @media(max-width:560px){.caps{grid-template-columns:1fr}}
 .capitem{display:flex;gap:10px;align-items:flex-start;background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px 13px}
 .capdot{flex:none;width:16px;height:16px;border-radius:50%;border:1px solid rgba(0,0,0,.12);margin-top:3px}
 .capitem b{font-size:14.5px;font-weight:normal} .captag{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#fff;background:var(--wine);border-radius:6px;padding:1px 6px;vertical-align:middle}
 .capwhy{font-size:12.5px;color:#7a7064}
 .capcard{text-decoration:none;color:inherit}
 .capthumb{flex:none;width:64px;height:80px;object-fit:cover;border-radius:8px;border:1px solid var(--line);background:#f4f1ec}
 .capmeta{min-width:0}
 .capbrand{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--wine);margin-top:3px}
 .capprice{font-size:12.5px;color:#7a7064;margin-top:2px}
 .capslot{margin:18px 0 8px} .capslotname{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--wine)}
 .shop{display:flex;flex-direction:column;gap:10px} .shopitem{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 16px}
 .shopname{font-size:16px;color:#2a2620} .shopwhy{font-size:13.5px;color:#5a5246;margin:3px 0 6px}
 .shoplinks{font-size:13px;color:#9a8f80} .shoplinks a{color:var(--wine);text-decoration:none}
 .ref{background:#fbf8f3;border:1px solid var(--line);border-radius:14px;padding:16px 20px}
 .refname{font-size:20px;color:var(--wine)} .refline{font-size:14px;color:#5a5246;margin:6px 0 0}
 .print{display:block;margin:30px auto 0;background:var(--wine);color:#fff;border:0;border-radius:10px;padding:14px 26px;font:inherit;font-size:16px;cursor:pointer}
 @media print{
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .bar,.print,#fbblock{display:none!important}
  body{background:var(--cream)} .wrap{max-width:none;padding:0}
  /* одна колонка на печати — двухколоночная сетка ломает разбивку и плодит белые листы */
  .looks,.caps{grid-template-columns:1fr!important;gap:12px}
  /* карточки и картинки не режем посреди страницы */
  .look,.capitem,.shopitem,.ref,.sw{break-inside:avoid;page-break-inside:avoid}
  .look img{break-inside:avoid}
  h2{break-after:avoid;page-break-after:avoid}
  /* каждый смысловой блок 02–04 — с новой страницы (у первого разрыв не нужен) */
  .blocklead{break-before:page;page-break-before:always;border-top:0;padding-top:0;margin:0 0 8px}
  .blocklead:first-of-type{break-before:avoid;page-break-before:avoid}
 }
</style></head><body><div class=wrap>
<div class=bar><a href="/me">← мой профиль</a><a href="/card?rebuild=1">собрать заново</a></div>
{% if stale %}<div class=stale><b>Твоя диагностика обновилась.</b> Ты недавно заново прошла квиз, и разрыв изменился. Эта Карта пока собрана на прежней диагностике — числа и подборка ниже от неё. Собери Карту заново, чтобы она совпала с последним квизом.<br><a href="/card?rebuild=1">Собрать Карту заново →</a></div>{% endif %}

<div class=eyebrow>Карта стиля</div>
<h1>Твоя Формула</h1>
{% if name %}<p class=who>для {{ name }}</p>{% endif %}
<p class=formula><b>{{ c.formula }}</b></p>
{% if c.season_label %}<p class=who style="margin:0 0 4px">Капсула на сезон: {{ c.season_label }}</p>{% endif %}
{% if c.gap is not none %}<p>Identity Gap: <span class=gap>{{ c.gap }}%</span> — тот самый разрыв с твоей диагностики. Здесь ты видишь, чем именно его закрыть.</p>{% endif %}
{% if c.dna %}<p class=dna>{{ c.dna }}</p>{% endif %}
{% macro lookcard(lk) %}<div class=look>
  {% if lk.img %}<img src="{{ lk.img }}" alt="Образ" style="width:100%;border-radius:10px;margin-bottom:10px;display:block">{% endif %}
  {% if lk.scenario %}<div class=scn>{{ lk.scenario }}</div>{% endif %}
  {% if lk.title %}<div class=nm>{{ lk.title }}</div>{% elif lk.name %}<div class=nm>{{ lk.name }}</div>{% endif %}
  {% if lk['items'] %}<p class=it>{{ lk['items']|join(' · ') }}</p>{% endif %}
  {% if lk.description %}<p class=ds>{{ lk.description }}</p>{% endif %}
 </div>{% endmacro %}

<!-- ═══════ БЛОК 1 · КТО ТЫ ═══════ -->
{% if c.substyle_rationale or (c.personality and c.personality.portrait) %}
<div class=blocklead><b>01</b>Кто ты</div>
<h2>Почему это твой стиль</h2>
{% if c.substyle_rationale %}<p style="font-size:16px;color:#3a352e;margin:0 0 12px">{{ c.substyle_rationale }}</p>{% endif %}
{% if c.personality and c.personality.portrait %}<p style="font-size:16px;color:#3a352e;margin:0 0 12px">{{ c.personality.portrait }}</p>
{% if c.personality.style_implications %}<ul class=clean>{% for s in c.personality.style_implications %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}{% endif %}
{% endif %}

<!-- ═══════ БЛОК 2 · ТВОИ ОБРАЗЫ ═══════ -->
{% if c.looks %}
<div class=blocklead><b>02</b>Твои образы</div>
<h2>Образы под твою жизнь</h2>
<p class=meta>Ты в своей Формуле — видно, как одни и те же базовые вещи работают в разных ситуациях.</p>
{% set ns = namespace(shown=0) %}
{% for bucket in ['Работа','Повседневное','Выход'] %}
 {% set bl = c.looks|selectattr('bucket','equalto',bucket)|list %}
 {% if bl %}{% set ns.shown = ns.shown + bl|length %}<h3 class=cap-h>{{ bucket }}</h3><div class=looks>{% for lk in bl %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}
{% endfor %}
{% if ns.shown == 0 %}<div class=looks>{% for lk in c.looks %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}

{% if c.styling and c.styling.looks %}<h2>Стилизация: одна вещь — два образа</h2>
<p class=meta>{% if c.styling.idea %}{{ c.styling.idea }}{% else %}Одна базовая вещь{% if c.styling.base_item %} ({{ c.styling.base_item }}){% endif %} — два разных образа.{% endif %} Так работает капсула: мало вещей, много решений.</p>
<div class=looks>{% for lk in c.styling.looks %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}
{% endif %}

<!-- ═══════ БЛОК 3 · ТВОЯ ФИГУРА ═══════ -->
{% if c.figure or c.figure_fit or c.silhouettes %}
<div class=blocklead><b>03</b>Твоя фигура</div>
<h2>Что носить по твоей фигуре</h2>
{% if c.figure %}<p class=meta>Силуэт: <b>{{ c.figure }}</b> — по этим правилам подобраны образы выше и капсула ниже, чтобы вещи сидели по твоим пропорциям.</p>{% endif %}
<ul class=clean>
 {% if c.emphasize %}<li><b>Твой акцент:</b> {{ c.emphasize }} — образы строим вокруг этого</li>{% endif %}
 {% if c.figure_fit %}<li><b>Подчёркиваем:</b> {{ c.figure_fit.emphasize }}</li>
 <li><b>Баланс:</b> {{ c.figure_fit.balance }}</li>
 <li><b>Посадка и размеры:</b> {{ c.figure_fit.fit }}</li>{% endif %}
</ul>
{% set sils = c.figure_fit.silhouettes if (c.figure_fit and c.figure_fit.silhouettes) else c.silhouettes %}
{% if sils %}<p class=meta style="margin:12px 0 6px">Твои силуэты:</p>
<ul class=clean>{% for s in sils %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}{% endif %}

<!-- ═══════ БЛОК 4 · ТВОИ ЦВЕТА ═══════ -->
<div class=blocklead><b>04</b>Твои цвета</div>
{% if c.colortype %}<h2>Твой цветотип — {{ c.colortype }}</h2>
<p class=meta>{% if c.contrast %}Контраст {{ c.contrast }}. {% endif %}На нём построена палитра ниже.</p>{% endif %}
<h2>Палитра — 30 цветов</h2>
<p class=meta>База — основа гардероба, на ней строится всё. Основные — цветные вещи, спокойно сочетаются с базой. Акценты — точечно: один на образ, не больше.</p>
{% for grp, title in [('base','База и нейтрали'),('main','Основные'),('accent','Акценты')] %}
 {% set items = c.palette|selectattr('group','equalto',grp)|list %}
 {% if items %}<div class=sw-group>{{ title }}</div><div class=swatches>
  {% for p in items %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}
 </div>{% endif %}
{% endfor %}
{% set rest = c.palette|rejectattr('group','in',['base','main','accent'])|list %}
{% if rest %}<div class=sw-group>Ещё в палитре</div><div class=swatches>
 {% for p in rest %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}
</div>{% endif %}

{% if c.stop_colors %}<h2>Стоп-цвета — что тебя гасит</h2>
<div class="swatches stopcolors">
 {% for p in c.stop_colors %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm><b>{{ p.name }}</b><br>{{ p.why }}</div></div>{% endfor %}
</div>{% endif %}

<!-- ═══════ БЛОК 5 · ТВОЙ ГАРДЕРОБ ═══════ -->
{% if c.visual_capsule or c.base_capsule %}
<div class=blocklead><b>05</b>Твой гардероб</div>
<h2>Базовая капсула — ядро гардероба</h2>
<p class=meta>Эти вещи — основа, всё остальное собирается вокруг них{% if c.combination_count %}: из них получается около {{ c.combination_count }} рабочих образов{% endif %}.</p>
{% if c.visual_capsule %}
 {% for grp in c.visual_capsule %}
 <div class=capslot><span class=capslotname>{{ grp.slot }}</span></div>
 <div class=caps>
  {% for it in grp['items'] %}{% if it.url %}<a class="capitem capcard" href="{{ it.url }}" target=_blank rel=noopener>{% else %}<div class="capitem capcard">{% endif %}
   {% if it.image %}<img class=capthumb src="{{ it.image }}" alt="{{ it.name }}">{% endif %}
   <div class=capmeta><b>{{ it.name }}</b>{% if it.brand %}<div class=capbrand>{{ it.brand }}</div>{% endif %}{% if it.price %}<div class=capprice>{{ '{:,}'.format(it.price).replace(',',' ') }} ₽</div>{% endif %}</div>
  {% if it.url %}</a>{% else %}</div>{% endif %}{% endfor %}
 </div>
 {% endfor %}
{% elif c.capsule_board %}
 {% for grp in c.capsule_board %}
 <div class=capslot><span class=capslotname>{{ grp.slot }}</span></div>
 <div class=caps>
  {% for it in grp['items'] %}<div class=capitem>
   {% if it.color and it.color.hex %}<span class=capdot style="background:{{ it.color.hex }}"></span>{% endif %}
   <div><b>{{ it.name }}</b>{% if it.role == 'base' %} <span class=captag>база</span>{% endif %}{% if it.why %}<br><span class=capwhy>{{ it.why }}</span>{% endif %}</div>
  </div>{% endfor %}
 </div>
 {% endfor %}
{% else %}
<div class=caps>
 {% for it in c.base_capsule %}<div class=capitem>
  {% if it.color and it.color.hex %}<span class=capdot style="background:{{ it.color.hex }}"></span>{% endif %}
  <div><b>{{ it.name }}</b>{% if it.role == 'base' %} <span class=captag>база</span>{% endif %}{% if it.why %}<br><span class=capwhy>{{ it.why }}</span>{% endif %}</div>
 </div>{% endfor %}
</div>{% endif %}{% endif %}

<!-- ═══════ БЛОК 6 · ЧТО КУПИТЬ ═══════ -->
{% if c.shopping or c.style_reference or c.stop_list %}<div class=blocklead><b>06</b>Что купить</div>{% endif %}
{% if c.shopping %}<h2>Топ покупок под твою Формулу</h2>
<div class=shop>
 {% for it in c.shopping %}<div class=shopitem>
  <div class=shopname>{{ it.item_name }}</div>
  {% if it.closes_gap %}<div class=shopwhy>{{ it.closes_gap }}</div>{% endif %}
  {% if it.links %}<div class=shoplinks>Найти: <a href="{{ it.links.wildberries }}" target=_blank rel=noopener>WB</a> · <a href="{{ it.links.lamoda }}" target=_blank rel=noopener>Lamoda</a> · <a href="{{ it.links.ozon }}" target=_blank rel=noopener>Ozon</a></div>{% endif %}
 </div>{% endfor %}
</div>
{% if c.budget and c.budget.min %}<p class=meta>Ориентир по бюджету: {{ '{:,}'.format(c.budget.min).replace(',',' ') }}–{{ '{:,}'.format(c.budget.max).replace(',',' ') }} ₽{% if c.budget.note %} · {{ c.budget.note }}{% endif %}</p>{% endif %}{% endif %}

{% if c.style_reference %}<h2>Стилевой ориентир</h2>
<div class=ref>
 <div class=refname>{{ c.style_reference.name }}</div>
 {% if c.style_reference.match_axis_1_impression %}<p class=refline>По впечатлению: {{ c.style_reference.match_axis_1_impression }}</p>{% endif %}
 {% if c.style_reference.match_axis_2_physical %}<p class=refline>По параметрам: {{ c.style_reference.match_axis_2_physical }}</p>{% endif %}
</div>{% endif %}

{% if c.stop_list %}<h2>Стоп-лист — что не носить</h2>
<ul class="clean stop">{% for s in c.stop_list %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}

<button class=print id=pdfbtn onclick="downloadPdf()">Скачать PDF к шкафу</button>

<div id=fbblock style="margin-top:38px;padding:22px;border:1px solid var(--line,#e3dccf);border-radius:14px;background:#fff">
{% if thanks %}
  <p style="margin:0;font-size:16px">Спасибо. Твой отзыв записан — он помогает делать Карту точнее.</p>
{% else %}
  <h2 style="margin:0 0 4px">Как тебе Карта?</h2>
  <p style="margin:0 0 14px;color:var(--muted,#6b645c);font-size:14px">Оцени и напиши пару слов — что откликнулось, чего не хватило.</p>
  <form method=post action="/card/feedback">
   <div style="display:flex;gap:10px;margin-bottom:12px">
    {% for n in [1,2,3,4,5] %}<label style="display:inline-flex;align-items:center;gap:5px;font-size:14px"><input type=radio name=rating value="{{ n }}" style="width:auto">{{ n }}</label>{% endfor %}
   </div>
   <textarea name=text rows=3 placeholder="Что откликнулось, чего не хватило?" style="width:100%;padding:11px 13px;border:1px solid #d9d2c7;border-radius:10px;font:inherit;font-size:15px"></textarea>
   <button type=submit style="margin-top:12px;padding:12px 24px;background:var(--wine,#5D2230);color:#fff;border:0;border-radius:10px;font:inherit;font-size:15px;cursor:pointer">Отправить отзыв</button>
  </form>
{% endif %}
</div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<script>
// Ждём загрузку шрифтов и ДЕКОДИРОВАНИЕ всех картинок до снимка html2canvas. Без этого частая
// беда PDF: заголовки в запасном шрифте и пустые прямоугольники вместо фото (снимок сделан раньше,
// чем картинки декодировались). Все фото Карты — data-URL, но крупные всё равно требуют decode.
function _ready(){
  var waits=[];
  if(document.fonts && document.fonts.ready){ waits.push(document.fonts.ready); }
  var imgs=Array.prototype.slice.call(document.querySelectorAll('.wrap img'));
  imgs.forEach(function(img){
    var dec=function(){ return img.decode ? img.decode().catch(function(){}) : Promise.resolve(); };
    if(img.complete && img.naturalWidth>0){ waits.push(dec()); }
    else { waits.push(new Promise(function(res){ img.onload=function(){ dec().then(res); }; img.onerror=res; })); }
  });
  return Promise.all(waits);
}
function downloadPdf(){
  var btn=document.getElementById('pdfbtn'); var bar=document.querySelector('.bar');
  var fb=document.getElementById('fbblock');
  var restore=function(){ if(bar) bar.style.visibility='visible'; btn.style.visibility='visible';
    if(fb) fb.style.display=''; btn.textContent='Скачать PDF к шкафу'; btn.disabled=false; };
  btn.textContent='Готовлю файл…'; btn.disabled=true;
  if(bar) bar.style.visibility='hidden'; btn.style.visibility='hidden'; if(fb) fb.style.display='none';
  var opt={margin:[12,12,14,12], filename:'Карта-стиля.pdf', image:{type:'jpeg',quality:0.96},
    html2canvas:{scale:2,useCORS:true,backgroundColor:'#F5EFE3',windowWidth:820,imageTimeout:15000},
    jsPDF:{unit:'mm',format:'a4',orientation:'portrait'},
    // css — уважать break-before блоков; avoid-all — не резать карточки. legacy убран: плодил пустые листы
    pagebreak:{mode:['css','avoid-all']}};
  _ready().then(function(){
    return html2pdf().set(opt).from(document.querySelector('.wrap')).save();
  }).then(restore).catch(restore);
}
</script>
</body></html>"""


CARD_BUILD_FORM = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Собрать Карту стиля</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:560px;margin:0 auto;padding:34px 22px 70px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-family:'Cormorant Garamond',serif;font-size:22px} .top a{color:var(--muted);font-size:14px;text-decoration:none}
 .eyebrow{font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--wine);margin:26px 0 10px}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:36px;line-height:1.08;margin:0 0 12px} .lead{color:var(--muted);margin:0 0 8px;font-size:15px}
 .card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:8px 22px 26px;margin-top:16px}
 label{display:block;margin:20px 0 7px;font-size:14px;font-weight:500;color:var(--ink)}
 .fld{width:100%;padding:12px 13px;border:1px solid #d9d2c7;border-radius:10px;font-family:inherit;font-size:15px;color:var(--ink);background:#fff;transition:border-color .15s}
 .fld:focus{outline:0;border-color:var(--wine)} .fld::placeholder{color:#a89f92}
 select.fld{appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%236b645c' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:34px}
 .stylegrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:4px 0 6px}
 .stylecard{position:relative;margin:0;cursor:pointer;border:1px solid var(--line);border-radius:12px;overflow:hidden;background:#fff;transition:border-color .15s,box-shadow .15s}
 .stylecard input{position:absolute;opacity:0;pointer-events:none}
 .stylepic{display:block;aspect-ratio:3/4;background-size:cover;background-position:top center;background-color:#eee6d8}
 .stylemeta{display:block;padding:8px 10px;font-size:14px} .stylehint{color:var(--muted);font-size:12px}
 .stylecard:has(input:checked){border-color:var(--wine);box-shadow:0 0 0 2px var(--wine)}
 .stylecard:has(input:checked)::after{content:'✓';position:absolute;top:8px;right:8px;width:24px;height:24px;border-radius:50%;background:var(--wine);color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px}
 .chips{display:flex;flex-wrap:wrap;gap:8px;margin:2px 0 4px}
 .chip{position:relative;cursor:pointer;margin:0}
 .chip input{position:absolute;opacity:0;pointer-events:none}
 .chip span{display:inline-block;padding:9px 15px;border:1px solid #d9d2c7;border-radius:999px;font-size:14px;color:var(--ink);background:#fff;transition:background .15s,color .15s,border-color .15s;user-select:none}
 .chip input:checked+span{background:var(--wine);color:#fff;border-color:var(--wine)}
 .chip input:focus-visible+span{box-shadow:0 0 0 2px rgba(93,34,48,.35)}
 .file{border:1.5px dashed #cdbfa6;border-radius:10px;padding:16px;text-align:center;background:#fbf8f1}
 input[type=file]{width:100%}
 button{margin-top:26px;width:100%;padding:15px;background:var(--wine);color:#fff;border:0;border-radius:10px;font-family:inherit;font-size:17px;cursor:pointer}
 .consent{font-size:13px;color:var(--muted);display:flex;gap:8px;margin-top:14px;line-height:1.4} .consent input{width:auto;margin-top:3px}
 .hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px} .hint a{color:var(--wine)}
 .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:8px}
 .notice{color:#5a4a2a;background:#f6efdf;border:1px solid #e3d3a8;padding:14px 16px;border-radius:10px;margin-bottom:8px;font-size:14.5px;line-height:1.5} .notice b{color:var(--wine)}
</style></head><body><div class=wrap>
{% macro chips(name, opts) %}<div class=chips>{% for o in opts %}<label class=chip><input type=checkbox name="{{ name }}" value="{{ o }}"><span>{{ o }}</span></label>{% endfor %}</div>{% endmacro %}
<div class=top><span class=logo>Чувство стиля</span><a href="/me">← мой профиль</a></div>
<div class=eyebrow>Карта стиля</div>
<h1>Покажем тебя в 6 образах</h1>
<p class=lead>Загрузи фото в полный рост — соберём твою Карту стиля и покажем тебя в 6 образах под твои сценарии. Это занимает пару минут.</p>
{% if notice %}<p class=notice>{{ notice|safe }}</p>{% endif %}
{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/card/build" enctype="multipart/form-data">
<div class=card>
 <label>Фото (в полный рост)</label>
 <div class=file><input type=file name=photo accept="image/*" required></div>
 <p class=hint style="text-align:left;margin:6px 0 0">Лицо должно быть хорошо видно — крупно, при дневном свете, без тёмных очков и сильной тени. От этого зависит сходство в образах.</p>
 <div class=eyebrow style="margin:24px 0 2px">Чтобы Карта была точнее (по желанию)</div>
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
 <label>{% if current_colortype_label %}Твой цветотип по фото — <b>{{ current_colortype_label }}</b>. Если знаешь свой сезон и он другой, выбери его — палитра пересоберётся:{% else %}Знаешь свой цветотип? Выбери сезон, и палитра соберётся под него (по желанию):{% endif %}</label>
 <select name=colortype_override class=fld>
  <option value="">{% if current_colortype_label %}— оставить как есть —{% else %}— определим по фото —{% endif %}</option>
  {% for code, lab in colortype_options %}<option value="{{ code }}">{{ lab }}</option>{% endfor %}
 </select>
 <label>На какой сезон собрать капсулу?</label>
 <div class=chips>
  <label class=chip><input type=radio name=season value=spring><span>Весна</span></label>
  <label class=chip><input type=radio name=season value=summer><span>Лето</span></label>
  <label class=chip><input type=radio name=season value=autumn checked><span>Осень</span></label>
  <label class=chip><input type=radio name=season value=winter><span>Зима</span></label>
 </div>
 <label>Что в твоей внешности подчеркнуть? Отметь, что нравится.</label>
 {{ chips('adv', ['талию','ноги','плечи','шею и декольте','запястья','осанку','грудь','бёдра']) }}
 <label>Что визуально уравновесить?</label>
 {{ chips('balance', ['плечи и бёдра','талию','добавить рост','смягчить плечи','объём сверху','объём снизу']) }}
 <label>Что ты точно не носишь? Уберём из образов.</label>
 {{ chips('taboo', ['мини','глубокое декольте','каблук выше 5 см','обтягивающее','яркие принты','красный','прозрачное','оверсайз']) }}
 <label>Чьё мнение учитываем в стиле?</label>
 {{ chips('audience', ['только своё','партнёр','дети','коллеги','родители']) }}

 <div class=eyebrow style="margin:26px 0 2px">Пара вопросов о тебе (по желанию)</div>
 <p style="font-size:13px;color:var(--muted);margin:0 0 10px">По шкале: 1 — совсем не про меня, 5 — точно про меня. Это поможет собрать образы под твою натуру.</p>
 {% for i, q in big5_questions %}
  <div style="margin:12px 0">
   <div style="font-size:14px">{{ q[2] }}</div>
   <div style="display:flex;gap:16px;margin-top:6px">
    {% for n in [1,2,3,4,5] %}<label style="font-size:13px;color:var(--muted);display:inline-flex;gap:4px;align-items:center;font-weight:normal;margin:0"><input type=radio name="b5_{{ i }}" value="{{ n }}" style="width:auto">{{ n }}</label>{% endfor %}
   </div>
  </div>
 {% endfor %}

 <div class=eyebrow style="margin:24px 0 2px">Твой круг жизни (по желанию)</div>
 <p style="font-size:13px;color:var(--muted);margin:0 0 8px">Сколько примерно времени в неделю (%) — чтобы образы попали в реальную жизнь.</p>
 <div style="display:flex;gap:10px">
  <label style="flex:1;margin:6px 0 0;font-size:13px;font-weight:400;color:var(--muted)">Работа<input type=number name=life_work min=0 max=100 placeholder="%" class=fld style="margin-top:4px"></label>
  <label style="flex:1;margin:6px 0 0;font-size:13px;font-weight:400;color:var(--muted)">Дом<input type=number name=life_home min=0 max=100 placeholder="%" class=fld style="margin-top:4px"></label>
  <label style="flex:1;margin:6px 0 0;font-size:13px;font-weight:400;color:var(--muted)">Свободное<input type=number name=life_free min=0 max=100 placeholder="%" class=fld style="margin-top:4px"></label>
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
 <button>Собрать Карту стиля →</button>
 <p class=hint>Фото нужно только для генерации образов и <b>удаляется сразу после сборки</b> — храним лишь готовые образы. <a href="/card?text=1">Собрать пока без образов (только текст)</a></p>
</form></div></body></html>"""


CARD_BUILDING = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Собираем Карту стиля…</title>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c}
 body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}
 .box{max-width:440px;padding:30px}
 h1{font-weight:normal;font-size:28px;margin:0 0 10px} p{color:var(--muted)}
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
    else if(d.status==='error'||d.status==='unknown'){
      document.getElementById('sp').style.display='none';
      document.getElementById('msg').innerHTML='<span class=err>Не удалось собрать: '+(d.error||'ошибка')+'</span><br><br><a href="/card?rebuild=1">Попробовать снова</a>';
    } else { setTimeout(poll, 4000); }
  }).catch(function(){ setTimeout(poll, 4000); });
}
setTimeout(poll, 3000);
</script>
</div></body></html>"""


STYLIST_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Стилист — Чувство стиля</title>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);height:100vh;display:flex;flex-direction:column}
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


@app.get("/privacy")
def privacy():
    return render_template_string(PRIVACY)


# ── Блог /blog — дом контента и SEO; статьи из content/blog/*.md ──────────────────
_BLOG_DIR = Path(__file__).resolve().parent.parent / "content" / "blog"

_BLOG_FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400;1,500&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">')

_BLOG_CSS = (
    ":root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#7A1C2E;--muted:#6b645c;--line:#e3dccf;--ph:#e9e0d0}"
    "*{box-sizing:border-box}body{margin:0;background:var(--cream);color:var(--ink);font-family:Onest,Georgia,serif;line-height:1.6}"
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
    email = session.get("email")
    if not email:
        return redirect("/login")
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    track = gap_progress(email)  # трекер разрыва: точки-замеры + дельта только при ≥2 замерах
    if track:  # человекочитаемые даты точек (точка отсчёта — первая)
        for p in track["points"]:
            p["date"] = _ru_date(p["ts"])
    return render_template_string(
        ME_PAGE, email=email, has_diag=bool(diag.get("style_formula")),
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
    # products_wb.csv — обувь, сумки и верхняя одежда (у Lichi/Ushatava их нет вовсе, слот «Обувь»
    # в капсуле пустовал). Файл собирается scripts/scrape_wb.py; если его нет — каталог работает
    # на том, что есть.
    for fname in ("products_ushatava.csv", "products_lichi.csv", "products_wb.csv"):
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
                    "купальник", "плавк", "чулк", "носки", "колготк", "халат", "сорочка ночная")


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
    profile = {
        "palette": card.get("palette") or [],
        "stop_list": card.get("stop_list") or [],
        "figure_type": diag.get("figure_type"),
        "base_style": (diag.get("style_dominant") or diag.get("base_style") or ""),
        "styles": styles,
        "gender": "женский",
    }
    # Ранжируем ВЕСЬ каталог под профиль, чтобы в каждом слоте был выбор, и раскладываем по слотам
    # (порядок внутри слота = релевантность). Предметное фото вперёд: в капсуле нужна сама вещь.
    ranked = _dedup_products(match_products(profile, products, k=len(products)))
    ranked = [p for p in ranked if _is_capsule_worthy(p.name)]
    by_slot: dict[str, list] = {}
    for p in ranked:
        by_slot.setdefault(_capsule_slot(p.category, p.name), []).append(p)
    for slot in by_slot:
        by_slot[slot].sort(key=lambda p: 0 if (p.image_kind or "") == "packshot" else 1)

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
                                   "price": int(p.price) if p.price else None} for p in picked[s]]}
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
<meta name=viewport content="width=device-width, initial-scale=1"><title>Мой гардероб</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Onest,-apple-system,Segoe UI,sans-serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:860px;margin:0 auto;padding:30px 22px 80px}
 .top{display:flex;justify-content:space-between;align-items:center} .logo{font-family:'Cormorant Garamond',serif;font-size:22px} .top a{color:var(--muted);font-size:14px;text-decoration:none;margin-left:16px}
 h1{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:38px;margin:20px 0 2px}
 .sub{color:var(--muted);margin:0} .sub .gap{color:var(--wine);font-weight:600}
 h2{font-family:'Cormorant Garamond',serif;font-weight:600;font-size:26px;margin:34px 0 4px}
 .hint{color:var(--muted);font-size:14px;margin:2px 0 14px}
 .seasons{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0 6px}
 .seasons a{padding:8px 15px;border:1px solid var(--line);border-radius:999px;font-size:14px;color:var(--ink);text-decoration:none;background:#fff}
 .seasons a.on{background:var(--wine);color:#fff;border-color:var(--wine)}
 .build{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:10px}
 @media(max-width:680px){.build{grid-template-columns:1fr}}
 .panel{background:#fff;border:1px solid var(--line);border-radius:16px;padding:16px 18px}
 .slot{margin:0 0 14px} .slotname{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine);margin:0 0 7px}
 .items{display:grid;grid-template-columns:repeat(auto-fill,minmax(82px,1fr));gap:8px}
 .pitem{cursor:grab;border:1px solid #e3dccf;border-radius:10px;background:#fff;padding:5px;text-align:center;user-select:none;transition:all .12s}
 .pitem:hover{border-color:var(--wine)} .pitem.on{border-color:var(--wine);box-shadow:0 0 0 2px var(--wine)}
 .pitem img{width:100%;aspect-ratio:3/4;object-fit:cover;border-radius:6px;display:block;background:#f2ede3}
 .pitem .ph{width:100%;aspect-ratio:3/4;border-radius:6px;background:#efe8db}
 .pitem .pname{display:block;font-size:10.5px;color:#4a443c;margin-top:5px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .canvas{position:sticky;top:14px}
 .cell{display:flex;align-items:center;gap:10px;border:1px dashed #cdbfa6;border-radius:12px;padding:9px 12px;margin:0 0 9px;background:#fbf8f1;transition:all .12s;min-height:56px}
 .cell.filled{border-style:solid;border-color:var(--wine);background:#fff}
 .cell.drop{border-color:var(--wine);background:#fdeee2}
 .cellslot{flex:0 0 96px;font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
 .cellbody{flex:1;display:flex;align-items:center;gap:10px}
 .cellbody .thumb{width:38px;aspect-ratio:3/4;object-fit:cover;border-radius:5px;background:#f2ede3;flex:0 0 auto}
 .cellval{font-size:14px;color:var(--ink)} .cell.filled .cellval{font-weight:500}
 .cellbody .buy{margin-left:auto;font-size:12.5px;color:var(--wine);text-decoration:none;white-space:nowrap}
 .itemtoggle{display:flex;gap:8px;margin:0 0 12px} .itemtoggle a{font-size:13px;padding:6px 13px;border:1px solid var(--line);border-radius:999px;text-decoration:none;color:var(--ink);background:#fff}
 .itemtoggle a.on{background:var(--wine);color:#fff;border-color:var(--wine)}
 .ctrls{display:flex;gap:10px;align-items:center;margin-top:12px}
 .ctrls button{font:inherit;font-size:14px;padding:9px 15px;border-radius:9px;cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--wine)}
 .ctrls .cnt{color:var(--muted);font-size:13px}
 .pal{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 2px}
 .pal .c{width:34px;height:34px;border-radius:8px;border:1px solid rgba(0,0,0,.08)}
 .palgrp{margin:14px 0 0;font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--wine)}
 .palhint{display:block;text-transform:none;letter-spacing:0;font-size:12.5px;color:var(--muted);margin-top:2px}
 .shop{display:flex;flex-direction:column;gap:10px;margin-top:6px} .shopitem{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 15px}
 .shopname{font-size:15.5px} .shopwhy{font-size:13px;color:var(--muted);margin:2px 0 0}
 .empty{color:var(--muted);font-size:14px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px}
 /* ── дашборд-хиро: сводка сверху (KPI + кольцо разрыва + трекер) ── */
 .dash{display:grid;grid-template-columns:auto 1fr;gap:22px;align-items:center;background:#fff;
  border:1px solid var(--line);border-radius:20px;padding:22px 24px;margin:18px 0 4px}
 .ring{position:relative;width:132px;height:132px;flex:0 0 auto}
 .ring svg{transform:rotate(-90deg)} .ring circle{transition:stroke-dashoffset 1.1s cubic-bezier(.22,1,.36,1)}
 .ring .rc{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
 .ring .rn{font-family:'Cormorant Garamond',serif;font-size:40px;line-height:1;color:var(--wine);font-weight:600}
 .ring .rl{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin-top:3px}
 .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
 .kpi{background:#fbf8f1;border:1px solid var(--line);border-radius:14px;padding:13px 15px}
 .kpi .kn{font-family:'Cormorant Garamond',serif;font-size:30px;line-height:1;color:var(--ink);font-variant-numeric:tabular-nums}
 .kpi .kl{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-top:6px;line-height:1.3}
 .kpi.wide{grid-column:1/-1;background:#fff}
 .kpi.wide .kn{font-size:20px}
 .delta{display:inline-block;background:#eef6ee;color:#3a5a3a;font-size:11.5px;padding:3px 9px;border-radius:999px;margin-left:8px;vertical-align:middle}
 .trk{background:#fff;border:1px solid var(--line);border-radius:20px;padding:18px 22px;margin:12px 0 4px}
 .trk .th{display:flex;justify-content:space-between;align-items:baseline;gap:10px;margin-bottom:6px}
 .trk h2{margin:0} .trk .ts{color:var(--muted);font-style:italic;font-size:14px}
 .trow{display:flex;align-items:center;gap:12px;margin:11px 0;font-size:13px}
 .tdate{flex:0 0 132px;color:var(--muted)} .tdate b{color:var(--wine);font-weight:normal}
 .tbar{flex:1;height:12px;background:#efe8db;border-radius:999px;overflow:hidden}
 .tfill{display:block;height:100%;background:linear-gradient(90deg,var(--wine),#8a3346);border-radius:999px;
  width:0;transition:width 1s cubic-bezier(.22,1,.36,1)}
 .tval{flex:0 0 42px;text-align:right;font-variant-numeric:tabular-nums;color:var(--wine)}
 .tnote{font-size:13px;color:var(--muted);margin:10px 0 0;line-height:1.5}
 @media(max-width:680px){.dash{grid-template-columns:1fr;justify-items:center;text-align:center}
  .kpis{grid-template-columns:1fr 1fr;width:100%} .tdate{flex-basis:96px}}
 @media(prefers-reduced-motion:reduce){.ring circle,.tfill{transition:none}}
 /* ── профиль-идентичность («стилевая ДНК») ── */
 .prof{display:flex;gap:18px;align-items:center;background:linear-gradient(135deg,#fff,#fbf6ec);
  border:1px solid var(--line);border-radius:20px;padding:20px 22px;margin:6px 0 2px}
 .ava{flex:0 0 auto;width:64px;height:64px;border-radius:50%;background:var(--wine);color:#fff;
  font-family:'Cormorant Garamond',serif;font-size:30px;display:flex;align-items:center;justify-content:center}
 .profmain{flex:1;min-width:0}
 .profform{font-family:'Cormorant Garamond',serif;font-size:24px;line-height:1.1;color:var(--ink)}
 .profchips{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
 .pc{font-size:12px;padding:4px 11px;border:1px solid var(--line);border-radius:999px;color:var(--muted);background:#fff}
 .pc b{color:var(--wine);font-weight:500}
 .want{font-size:13.5px;color:var(--muted);margin-top:10px;font-style:italic}
 .want b{color:var(--ink);font-style:normal}
 .nudge{display:flex;gap:12px;align-items:center;background:#fbf3e8;border:1px solid #e8d9c2;
  border-radius:14px;padding:12px 16px;margin:12px 0 2px;font-size:13.5px;color:#7a5b32}
 .nudge a{margin-left:auto;white-space:nowrap;background:var(--wine);color:#fff;text-decoration:none;
  padding:8px 14px;border-radius:9px;font-size:13px;flex:0 0 auto}
 @media(max-width:680px){.prof{flex-direction:column;text-align:center}.profchips{justify-content:center}}
 /* ── отзыв клиентки ── */
 .fb{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px 22px;margin-top:14px}
 .fb h2{margin:0 0 4px} .fb p.h{color:var(--muted);font-size:14px;margin:0 0 14px}
 .stars{display:flex;gap:6px;margin-bottom:12px;flex-direction:row-reverse;justify-content:flex-end}
 .stars input{position:absolute;opacity:0;width:0;height:0}
 .stars label{font-size:26px;color:#d9cfbf;cursor:pointer;line-height:1;transition:color .12s}
 .stars label:hover,.stars label:hover~label,.stars input:checked~label{color:#c8a24a}
 .fb textarea{width:100%;padding:11px 13px;border:1px solid #d9d2c7;border-radius:10px;font:inherit;font-size:15px;resize:vertical}
 .fb button{margin-top:12px;padding:12px 24px;background:var(--wine);color:#fff;border:0;border-radius:10px;font:inherit;font-size:15px;cursor:pointer}
 .fb .done{margin:0;font-size:16px;color:var(--ink)}
 /* ── роли недели ── */
 .roles3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:6px 0 2px}
 .role3{background:#fff;border:1px solid var(--line);border-radius:14px;padding:15px 16px;display:flex;flex-direction:column}
 .role3 .rb{font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine)}
 .role3 .rn{font-family:'Cormorant Garamond',serif;font-size:19px;line-height:1.15;margin:5px 0 8px;color:var(--ink)}
 .role3 ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:4px}
 .role3 li{font-size:12.5px;color:var(--muted);line-height:1.3}
 .role3 li::before{content:"— ";color:var(--wine)}
 @media(max-width:680px){.roles3{grid-template-columns:1fr}}
 /* ── прогресс-вехи ── */
 .miles{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 2px}
 .mile{flex:1;min-width:120px;background:#fbf8f1;border:1px solid var(--line);border-radius:12px;padding:12px 14px}
 .mile .mn{font-family:'Cormorant Garamond',serif;font-size:26px;line-height:1;color:var(--ink);font-variant-numeric:tabular-nums}
 .mile .mn.win{color:#3a5a3a} .mile .ml{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-top:5px}
 /* ── липкая навигация по секциям (как таб-бар приложения) ── */
 .cabnav{position:sticky;top:0;z-index:20;background:rgba(245,239,227,.92);backdrop-filter:blur(8px);
  margin:0 -22px 8px;padding:10px 22px;border-bottom:1px solid var(--line);display:flex;gap:8px;
  overflow-x:auto;-webkit-overflow-scrolling:touch}
 .cabnav a{flex:0 0 auto;font-size:13px;color:var(--muted);text-decoration:none;padding:6px 13px;
  border-radius:999px;border:1px solid transparent;white-space:nowrap;transition:all .12s}
 .cabnav a:hover{color:var(--wine);border-color:var(--line);background:#fff}
 .sechead{scroll-margin-top:64px}
 /* ── карточка «Брать / не брать» ── */
 .checkc{display:flex;gap:18px;align-items:center;background:linear-gradient(135deg,#fff,#f7efe0);
  border:1px solid var(--line);border-radius:18px;padding:22px 24px;margin-top:6px}
 .checkc .ico{flex:0 0 auto;width:56px;height:56px;border-radius:16px;background:var(--wine);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:26px}
 .checkc .ct{flex:1;min-width:0}
 .checkc .ctt{font-family:'Cormorant Garamond',serif;font-size:22px;color:var(--ink);line-height:1.1}
 .checkc .cts{font-size:14px;color:var(--muted);margin-top:5px}
 .checkc .verd{display:flex;gap:6px;margin-top:9px;flex-wrap:wrap}
 .checkc .vt{font-size:11.5px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
 .checkc a.go{flex:0 0 auto;background:var(--wine);color:#fff;text-decoration:none;padding:12px 20px;
  border-radius:10px;font-size:14px;white-space:nowrap}
 @media(max-width:680px){.checkc{flex-direction:column;text-align:center}.checkc a.go{width:100%;text-align:center}}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><span><a href="/stylebook">Style Book</a><a href="/me">профиль</a><a href="/card">Карта</a><a href="/logout">выйти</a></span></div>
<h1>Мой гардероб</h1>

{# ── профиль-идентичность: кто ты по стилю (наша «стилевая ДНК») ── #}
<div class=prof>
 <div class=ava>{{ email[0]|upper }}</div>
 <div class=profmain>
  <div class=profform>{{ formula }}</div>
  <div class=profchips>
   {% if colortype %}<span class=pc>Цветотип · <b>{{ colortype }}</b></span>{% endif %}
   {% if figure %}<span class=pc>Силуэт · <b>{{ figure }}</b></span>{% endif %}
   {% if season_label %}<span class=pc>Сезон · <b>{{ season_label }}</b></span>{% endif %}
  </div>
  {% if want_traits %}<div class=want>Ты хочешь считываться как <b>{{ want_traits|join(', ') }}</b> — на это работает вся капсула ниже.</div>{% endif %}
 </div>
</div>

{# ── навигация по секциям кабинета ── #}
<nav class=cabnav>
 <a href="#dash">Обзор</a>
 {% if track %}<a href="#track">Трекер</a>{% endif %}
 {% if roles %}<a href="#roles">Роли недели</a>{% endif %}
 <a href="#wardrobe">Гардероб</a>
 <a href="#check">Брать / не брать</a>
 <a href="#shopping">Покупки</a>
 <a href="#review">Отзыв</a>
</nav>

{# ── дашборд: сводка одним взглядом ── #}
<div class=dash id=dash>
 <div class=ring>
  <svg width=132 height=132 viewBox="0 0 132 132" aria-hidden=true>
   <circle cx=66 cy=66 r=52 fill=none stroke="#efe8db" stroke-width=11></circle>
   <circle id=gapRing cx=66 cy=66 r=52 fill=none stroke="var(--wine)" stroke-width=11
     stroke-linecap=round stroke-dasharray=327
     stroke-dashoffset="{{ 327 if gap_now is none else (327 * (1 - gap_now / 100.0))|round(1) }}"></circle>
  </svg>
  <div class=rc><span class=rn>{% if gap_now is not none %}{{ gap_now }}%{% else %}—{% endif %}</span><span class=rl>разрыв сейчас</span></div>
 </div>
 <div class=kpis>
  <div class="kpi wide"><div class=kn>{{ formula }}</div><div class=kl>твоя формула стиля</div></div>
  <div class=kpi><div class=kn>{{ n_items }}</div><div class=kl>вещей в капсуле</div></div>
  <div class=kpi><div class=kn>{{ combos_label }}</div><div class=kl>образов из капсулы</div></div>
  <div class=kpi><div class=kn>{% if track %}{{ track.measurements }}{% else %}1{% endif %}{% if track and track.delta and track.delta > 0 %}<span class=delta>−{{ track.delta }} п.п.</span>{% endif %}</div><div class=kl>{% if track and track.measurements > 1 %}замера разрыва{% else %}точка отсчёта{% endif %}</div></div>
 </div>
</div>

{% if track %}
<div class=trk id=track>
 <div class=th><h2 class=sechead>Как закрывается разрыв</h2><span class=ts>измеримая трансформация</span></div>
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
 <p class=tnote>Это точка отсчёта. Сделай пере-замер через время — увидишь, как разрыв закрывается. Он двигается только от настоящего замера: новых фото того, как ты одеваешься сейчас.</p>
 {% else %}
 <p class=tnote>Разрыв закрывается — и это видно. Двигается он только от реального пере-замера, поэтому цифре можно верить.</p>
 {% endif %}
</div>
{% endif %}

{% if days_since is not none and days_since >= 30 %}
<div class=nudge><span>С последнего замера прошло {{ days_since }} дней. Пере-замер покажет, как разрыв закрылся за это время.</span><a href="/identity-scan-quiz.html?fresh=1">Сделать пере-замер</a></div>
{% endif %}

{% if roles %}
<h2 class=sechead id=roles>Роли твоей недели</h2>
<p class=hint>Одна капсула — разные роли твоего дня. Так формула работает под каждую жизненную ситуацию.</p>
<div class=roles3>
 {% for r in roles %}
 <div class=role3>
  <div class=rb>{{ r.bucket }}</div>
  <div class=rn>{% if r.name %}{{ r.name }}{% else %}{{ r.scenario }}{% endif %}</div>
  {% if r.pieces %}<ul>{% for it in r.pieces %}<li>{{ it }}</li>{% endfor %}</ul>{% endif %}
 </div>
 {% endfor %}
</div>
{% endif %}

<h2 class=sechead id=wardrobe>Твой капсульный гардероб</h2>
<p class=hint>Реальные вещи под твою Формулу и сезон — каждую можно купить. Ниже собери из них лук.</p>
{% if season_tabs %}
<div class=seasons>
 {% for s in season_tabs %}<a href="/cabinet?season={{ s.code }}" class="{{ 'on' if s.on else '' }}">{{ s.label }}</a>{% endfor %}
</div>
{% endif %}
<h2>Конструктор образа</h2>
<p class=hint>Собери лук из своей капсулы: перетащи или нажми вещь в каждый слот. Цвета берёшь из своей палитры — они выверены и сочетаются между собой. Вещи настоящие — каждую можно купить.</p>
<div class=itemtoggle>
 <a href="/cabinet?items=6{% if sel_season %}&season={{ sel_season }}{% endif %}" class="{{ 'on' if items_n == 6 else '' }}">Капсула 6 вещей</a>
 <a href="/cabinet?items=12{% if sel_season %}&season={{ sel_season }}{% endif %}" class="{{ 'on' if items_n == 12 else '' }}">Расширенная 12</a>
</div>
<div class=build>
 <div class=panel>
  {% for grp in board %}
  <div class=slot>
   <div class=slotname>{{ grp.slot }}</div>
   <div class=items>
    {% for it in grp['items'] %}<span class=pitem data-slot="{{ grp.slot }}" data-name="{{ it.name }}" data-img="{{ it.image or '' }}" data-url="{{ it.url or '' }}">{% if it.image %}<img src="{{ it.image }}" alt="" loading=lazy>{% else %}<span class=ph></span>{% endif %}<span class=pname>{{ it.name }}</span></span>{% endfor %}
   </div>
  </div>
  {% endfor %}
  {% if not board %}<p class=empty>Капсула ещё не собрана. <a href="/card">Собери Карту стиля</a> — вещи появятся здесь.</p>{% endif %}
 </div>
 <div class=panel canvas>
  <div class=slotname style="margin-bottom:10px">Твой лук</div>
  {% for grp in board %}
  <div class=cell data-cell="{{ grp.slot }}"><span class=cellslot>{{ grp.slot }}</span><span class=cellbody><span class=cellval>—</span></span></div>
  {% endfor %}
  <div class=ctrls><button type=button onclick=clearOutfit()>Очистить</button><span class=cnt>вещей в луке: <b id=count>0</b></span></div>
  {% if palette %}
  <div class=slotname style="margin:16px 0 6px">Твоя палитра</div>
  {% set grouped = palette|selectattr('group')|list %}
  {% if grouped %}
   {# цвета разложены по роли: обещать «всё сочетается со всем» нельзя — акценты работают точечно #}
   {% for grp, title, hint in [
       ('base','База и нейтрали','основа гардероба — на них строится всё остальное'),
       ('main','Основные','цветные вещи, спокойно сочетаются с базой'),
       ('accent','Акценты','точечно: один акцент на образ, не больше')] %}
    {% set items = palette|selectattr('group','equalto',grp)|list %}
    {% if items %}
    <div class=palgrp>{{ title }}<span class=palhint>{{ hint }}</span></div>
    <div class=pal>{% for p in items %}<span class=c style="background:{{ p.hex }}" title="{{ p.name }}"></span>{% endfor %}</div>
    {% endif %}
   {% endfor %}
   {# цвет с неожиданной ролью не должен молча исчезнуть с экрана #}
   {% set rest = palette|rejectattr('group','in',['base','main','accent'])|list %}
   {% if rest %}
   <div class=palgrp>Ещё в палитре</div>
   <div class=pal>{% for p in rest %}<span class=c style="background:{{ p.hex }}" title="{{ p.name }}"></span>{% endfor %}</div>
   {% endif %}
  {% else %}
   {# старая Карта без ролей цветов — показываем как есть, без обещаний про сочетаемость #}
   <div class=pal>{% for p in palette %}<span class=c style="background:{{ p.hex }}" title="{{ p.name }}"></span>{% endfor %}</div>
  {% endif %}
  {% endif %}
 </div>
</div>

<h2 class=sechead id=check>Брать или не брать?</h2>
<p class=hint>Стоишь в магазине с вещью в руках — сфотографируй, и я скажу, работает ли она на твою Формулу.</p>
<div class=checkc>
 <div class=ico>✓</div>
 <div class=ct>
  <div class=ctt>Проверка вещи по фото</div>
  <div class=cts>Оценю по твоему цветотипу, линиям фигуры и ядру стиля — до того, как ты потратишь деньги.</div>
  <div class=verd><span class=vt>✓ Брать</span><span class=vt>↺ Подумай</span><span class=vt>✕ Оставь в магазине</span></div>
 </div>
 <a class=go href="/garment">Проверить вещь →</a>
</div>

<h2 class=sechead id=shopping>Лист покупок</h2>
<p class=hint>Что докупить, чтобы закрыть разрыв — вещи под твою Формулу и сезон.</p>
{% if shopping %}
<div class=shop>
 {% for it in shopping %}<div class=shopitem><div class=shopname>{{ it.name }}</div>{% if it.closes_gap %}<div class=shopwhy>{{ it.closes_gap }}</div>{% endif %}</div>{% endfor %}
</div>
{% else %}<p class=empty>Лист покупок появится вместе с собранной Картой.</p>{% endif %}

<div class=fb id=review>
{% if thanks %}
 <p class=done>Спасибо. Твой отзыв записан — он помогает нам делать сервис точнее.</p>
{% else %}
 <h2 class=sechead>Как тебе твой гардероб?</h2>
 <p class=h>Оцени и напиши пару слов — что откликнулось, чего не хватило. Это помогает нам и другим клиенткам.</p>
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

<script>
var outfit={}, byKey={};
document.querySelectorAll('.pitem').forEach(function(i){
 byKey[i.getAttribute('data-slot')+'|'+i.getAttribute('data-name')]={
  slot:i.getAttribute('data-slot'), name:i.getAttribute('data-name'),
  img:i.getAttribute('data-img'), url:i.getAttribute('data-url')};
});
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
 document.getElementById('count').textContent=Object.keys(outfit).length;
}
function pickKey(key){ var o=byKey[key]; if(!o) return;
 if(outfit[o.slot] && outfit[o.slot].name===o.name){ delete outfit[o.slot]; } else { outfit[o.slot]=o; } render(); }
function clearOutfit(){ outfit={}; render(); }
document.querySelectorAll('.pitem').forEach(function(i){
 var key=i.getAttribute('data-slot')+'|'+i.getAttribute('data-name');
 i.setAttribute('draggable','true');
 i.addEventListener('click',function(){ pickKey(key); });
 i.addEventListener('dragstart',function(e){ e.dataTransfer.setData('text', key); });
});
document.querySelectorAll('[data-cell]').forEach(function(c){
 c.addEventListener('dragover',function(e){ e.preventDefault(); c.classList.add('drop'); });
 c.addEventListener('dragleave',function(){ c.classList.remove('drop'); });
 c.addEventListener('drop',function(e){ e.preventDefault(); c.classList.remove('drop');
  var key=e.dataTransfer.getData('text')||''; if(key.split('|')[0]===c.getAttribute('data-cell')) pickKey(key); });
});
// трекер и кольцо разрыва: анимируем от нуля к значению при загрузке. Значения уже проставлены
// в разметке (корректны без JS) — стартуем с пустого и возвращаем к финалу, transition доигрывает.
(function(){
 var ring=document.getElementById('gapRing'), fills=document.querySelectorAll('.tfill');
 var finalOff=ring?ring.style.strokeDashoffset:null;
 if(ring) ring.style.strokeDashoffset=327;               // пусто
 fills.forEach(function(b){ b.style.width='0%'; });
 requestAnimationFrame(function(){ requestAnimationFrame(function(){
  if(ring) ring.style.strokeDashoffset=finalOff;         // → к значению
  fills.forEach(function(b){ b.style.width=(b.getAttribute('data-w')||0)+'%'; });
 }); });
})();
</script>
</div></body></html>"""


@app.get("/cabinet")
def cabinet():
    """Кабинет: капсульный гардероб по сезонам + конструктор образов (верх/низ/обувь) + лист покупок."""
    email = session.get("email")
    if not email:
        return redirect("/login?next=/cabinet")
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/identity-scan-quiz.html?fresh=1")
    by_season = current_card_by_season(email)  # {код_сезона: карта} — последняя версия на сезон
    card = prof.get("card") or {}
    sel = (request.args.get("season") or "").strip()
    if sel in by_season:               # выбран сезон с собранной капсулой
        card = by_season[sel]
    else:
        sel = card.get("season") or ""
    if not card:
        return redirect("/card")       # капсулы ещё нет — сначала собрать Карту
    items_n = 6 if request.args.get("items") == "6" else 12  # капсула 6 / расширенная 12
    # визуальная капсула из реального каталога (фото+ссылки); фолбэк — текстовый борд из Карты
    board = _visual_capsule(card, diag, items_n) or \
        card.get("capsule_board") or _capsule_board(card.get("base_capsule") or [])
    seasons = [s for s in _SEASON_ORDER if s in by_season]
    if sel in _CARD_SEASONS and sel not in seasons:
        seasons.append(sel)
    season_tabs = [{"code": s, "label": _CARD_SEASONS[s]["label"], "on": s == sel}
                   for s in _SEASON_ORDER if s in seasons]
    palette = [p for p in (card.get("palette") or []) if p.get("hex")]
    # трекер разрыва прямо в дашборде (раньше жил только в /me): точки-замеры + дельта при ≥2
    track = gap_progress(email)
    days_since = None
    if track:
        for p in track["points"]:
            p["date"] = _ru_date(p["ts"])
        last_ts = track["points"][-1].get("ts")
        try:  # сколько дней прошло с последнего замера → мягкий призыв к пере-замеру
            from datetime import datetime, timezone
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
    # «Роли твоей недели»: по одному образу на жизненную капсулу (Работа/Повседневное/Выход)
    roles = []
    seen_buckets = set()
    for lk in (card.get("looks") or []):
        b = lk.get("bucket") or "Повседневное"
        if b in seen_buckets:
            continue
        seen_buckets.add(b)
        roles.append({"bucket": b, "scenario": lk.get("scenario") or b,
                      "name": lk.get("name"), "pieces": (lk.get("items") or [])[:4]})
    roles.sort(key=lambda r: ["Работа", "Повседневное", "Выход"].index(r["bucket"])
               if r["bucket"] in ("Работа", "Повседневное", "Выход") else 9)
    # «Прогресс-вехи»: старт, текущий, лучший разрыв, суммарная дельта (из трекера)
    milestones = None
    if track and track.get("points"):
        gaps = [p["gap"] for p in track["points"]]
        milestones = {"start": gaps[0], "now": gaps[-1], "best": min(gaps),
                      "delta": (gaps[0] - gaps[-1]) if len(gaps) > 1 else 0,
                      "count": track.get("measurements", len(gaps))}
    return render_template_string(
        CABINET_PAGE, email=email, roles=roles, milestones=milestones,
        formula=card.get("formula") or diag.get("style_formula"),
        colortype=_colortype_label(diag.get("colortype")), figure=_figure_label(diag.get("figure_type")),
        want_traits=want3, days_since=days_since, thanks=(request.args.get("fb") == "1"),
        gap=card.get("gap"), gap_now=gap_now, track=track, season_label=card.get("season_label"),
        n_items=n_items, combos_label=combos_label, items_n=items_n,
        board=board, palette=palette, shopping=card.get("shopping") or [],
        season_tabs=season_tabs, sel_season=sel)


STYLEBOOK_PAGE = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Персональный Style Book</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;0,600;1,500&family=Onest:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
 :root{--paper:#F5EFE3;--ink:#221f1d;--soft:#4c463f;--muted:#7a7168;--wine:#5D2230;--line:#e2dacd}
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
  </svg><div class=v><span class=n>{{ gap }}%</span><span class=l>разрыв · до</span></div></div>{% endif %}
  <p class=lead style="flex:1;min-width:260px">{{ dna or 'Твой образ догоняет то, кем ты становишься. Разрыв замеряется до и после — это ядро работы.' }}</p>
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
 <p class=lead>Реальные вещи под твою Формулу — ядро, из которого собираются все образы.</p>
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
<a class=back href="/cabinet">← Вернуться в кабинет</a>
</div></body></html>"""


@app.get("/stylebook")
def stylebook():
    """Фото-Style Book (пакет «Преображение»): книга из данных Карты с ФОТО образов на клиентке.
    Собирается из готовых выходов движка (палитра/капсула/образы) — без новых генераций.
    За гейтом оплаты (пока — админ / premium-флаг; upsell для остальных)."""
    email = session.get("email")
    if not email:
        return redirect("/login?next=/stylebook")
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
    email = session.get("email")
    profile = get_profile(email) if email else None
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


def build_style_card(diag: dict, season: str | None = None) -> dict:
    """Собрать продукт «Карта стиля» из Формулы: палитра 30 цветов + 6 образов + секции.
    Два текстовых вызова (палитра + капсула), без рендера картинок. season — ss|fw (капсула
    собирается под сезон); по умолчанию осень-зима."""
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
        for key in ("primary_substyle", "secondary_substyle", "accent_note", "style_formula"):
            if ref.get(key):
                diag[key] = ref[key]
        substyle_rationale = ref.get("substyle_rationale") or ""
    # Палитра и капсула — на flash (dev): надёжно и быстро. pro@final в проде отдаёт
    # finish_reason=error (нестабилен), поэтому для продукта НЕ используем (2026-06-29).
    palette = generate_card_palette(diag, mode="dev")
    scenarios = ["работа", "деловая встреча", "повседневное",
                 "событие и выход", "свидание", "путешествие"]
    gen_req = {"mode": "capsule", "capsule_type": "auto", "season": seas["gen"],
               "scenarios": scenarios, "n_looks": 6, "price_segment": price_segment,
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
    looks = _ensure_n_looks(capsule.get("looks") or [], scenarios, capsule, diag)
    for lk in looks:  # жизненная капсула: группируем сценарии в Работа/Повседневное/Выход
        lk["bucket"] = _LIFE_BUCKET.get((lk.get("scenario") or "").strip().lower(), "Повседневное")
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
        # базовая капсула (ядро) — вещи, из которых собираются все образы
        "base_capsule": [it for it in cap_items if isinstance(it, dict) and it.get("name")][:14],
        "capsule_board": _capsule_board([it for it in cap_items if isinstance(it, dict) and it.get("name")][:14]),
        # визуальная капсула: реальные вещи каталога с ФОТО (вшиты в data-URL, чтобы жили и в PDF).
        # фолбэк на текстовую капсулу выше, если каталог пуст/фото не тянутся.
        "visual_capsule": _inline_capsule_images(
            _visual_capsule({"palette": palette.get("palette") or [], "stop_list": stop_list_full}, diag, 10)),
        "combination_count": (capsule.get("capsule") or {}).get("combination_count"),
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
        em = session.get("email")
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


# Группировка 6 сценариев в 3 жизненные капсулы (Карта стиля)
_LIFE_BUCKET = {
    "работа": "Работа", "деловая встреча": "Работа",
    "повседневное": "Повседневное", "путешествие": "Повседневное",
    "событие и выход": "Выход", "свидание": "Выход",
}


def _ensure_n_looks(looks: list, scenarios: list, capsule: dict, diag: dict) -> list:
    """Гарантия ровно по одному образу на каждый сценарий (LLM иногда отдаёт меньше).
    Переиспользуем образы LLM, лишние переназначаем на пустые сценарии, недостающие
    дособираем из вещей капсулы — без дополнительных вызовов модели."""
    def _norm(s):
        return (s or "").strip().lower()

    by_scn, extras = {}, []
    for lk in looks:
        scn = _norm(lk.get("scenario"))
        match = next((s for s in scenarios if _norm(s) in scn or scn in _norm(s)), None)
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
                    "image_generation_prompt": lk.get("image_generation_prompt", "")})
    return out


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
        diag = (get_profile(email) or {}).get("diagnosis") or {}
        card = build_style_card(diag, season=season)
        # рендерим 6 образов карты + 2 образа стилизации (одна вещь → два образа)
        targets = list(card.get("looks") or []) + list((card.get("styling") or {}).get("looks") or [])

        def _render(lk):
            try:
                return render_look_on_client(str(photo_path), _card_look_prompt(lk, diag))
            except Exception:  # noqa: BLE001 — один неудавшийся образ не валит карту
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets))) as ex:
            imgs = list(ex.map(_render, targets))
        for lk, img in zip(targets, imgs):
            if img:
                lk["img"] = img
        save_card(email, card)  # храним готовые образы, не исходное фото
        record_event("card_built", email)
        record_event("look_generated", email, meta=str(sum(1 for i in imgs if i)))
        port = (card.get("personality") or {}).get("portrait")
        if port:  # портрет личности — в профиль, чтобы видел чат-стилист
            d2 = (get_profile(email) or {}).get("diagnosis") or {}
            d2["personality_portrait"] = port
            save_diagnosis(email, d2)
        _JOBS[job_id] = {"status": "done"}
    except Exception as e:  # noqa: BLE001
        _JOBS[job_id] = {"status": "error", "error": str(e)}
    finally:
        try:
            Path(photo_path).unlink()  # фото не храним (Политика)
        except OSError:
            pass


@app.get("/card")
def style_card():
    """Карта стиля. Готовая (кэш) → показываем; иначе форма загрузки фото для сборки.
    ?text=1 — собрать без образов (только текст, синхронно); ?rebuild=1 — пересобрать."""
    email = session.get("email")
    if not email:  # не вошла → на регистрацию, потом вернёмся сюда (с from_job)
        return redirect("/login?next=" + quote(request.full_path))
    # привязка диагностики из квиза (анонимный прошёл квиз → зарегистрировался → сюда)
    from_job = request.args.get("from_job")
    if from_job:
        # из памяти (быстро) или с диска (переживает рестарт сервера) — иначе была петля на квиз
        job_diag = (_JOBS.get(from_job) or {}).get("diag") or _load_pending_diag(from_job)
        if job_diag:
            save_diagnosis(email, job_diag)
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/identity-scan-quiz.html?fresh=1")  # сначала нужна диагностика (квиз)
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
    if card and not request.args.get("rebuild") and not request.args.get("text"):
        return render_template_string(STYLE_CARD, c=card, name=email,
                                      thanks=request.args.get("fb"), stale=False)
    # бесплатная генерация — один раз на email; пересборку/повтор блокируем (защита токенов).
    # Исключение: диагностика реально изменилась (новый квиз) — даём пересобрать Карту под неё.
    if (request.args.get("rebuild") or request.args.get("text")) and not _gen_allowed(email) and not stale:
        if card:
            return render_template_string(STYLE_CARD, c=card, name=email, thanks=None)
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
        return render_template_string(STYLE_CARD, c=card, name=email)
    record_event("card_form_view", email)
    return render_template_string(CARD_BUILD_FORM, error=None)


@app.post("/card/build")
def card_build():
    """Старт асинхронной сборки карты с образами на клиентке (фото → рендер → удаление)."""
    email = session.get("email")
    if not email:
        return redirect("/login")
    prof = get_profile(email) or {}
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/identity-scan-quiz.html?fresh=1")
    # бесплатная генерация — один раз на email (защита токенов). Исключение — устаревшая Карта
    # (клиентка заново прошла квиз): разрешаем пересобрать под новую диагностику.
    if not _gen_allowed(email) and not _card_stale(prof):
        return render_template_string(CARD_BUILD_FORM, error=_GEN_LIMIT_MSG), 429
    if not _quota_left():
        return render_template_string(CARD_BUILD_FORM, error="Лимит на сегодня исчерпан."), 429
    if not _consent_ok(request.form):
        return render_template_string(CARD_BUILD_FORM, error="Нужно согласие на обработку и передачу фото."), 400
    record_consent(email, request.remote_addr or "", True, True)
    try:
        photo_path = _validate_and_save(request.files.get("photo"))
    except ValueError as e:
        return render_template_string(CARD_BUILD_FORM, error=str(e)), 400
    _save_deep_intake(email, request.form)  # тело+возражения из анкеты → в Формулу/стоп-лист/чат
    record_call()
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
    return jsonify({"ok": True})


@app.post("/card/feedback")
def card_feedback():
    """Отзыв клиентки о Карте (оценка + текст). Питает артефакт «обратная связь» конкурса."""
    email = session.get("email")
    if not email:
        return redirect("/login")
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


# Лимит бесплатных генераций на email (защита от слива токенов). Админ — без лимита.
FREE_GEN_LIMIT = int(os.getenv("SENSE_FREE_GEN_LIMIT", "1"))
_GEN_LIMIT_MSG = ("Бесплатная генерация Карты уже использована по этой почте. "
                  "Твоя Карта сохранена — открой её в разделе «Мой профиль».")


def _gen_allowed(email: str) -> bool:
    """Можно ли этому email запускать генерацию Карты (в пределах бесплатного лимита)."""
    if _is_admin():
        return True
    return count_generations(email) < FREE_GEN_LIMIT


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

<h2>Воронка</h2>
<div class=grid>
 <div class=kpi><b>{{ f.quiz_done }}</b><span>прошли квиз (диагностик)</span></div>
 <div class=kpi><b>{{ f.unique_clients }}</b><span>уникальных клиенток</span></div>
 <div class=kpi><b>{{ f.card_form_view }}</b><span>открыли форму Карты</span></div>
 <div class=kpi><b>{{ f.card_built }}</b><span>собрали Карту</span></div>
 <div class=kpi><b>{{ f.quiz_to_card_pct }}%</b><span>квиз → Карта</span></div>
 <div class=kpi><b>{{ f.looks_generated }}</b><span>прогонов генерации образов</span></div>
</div>
<h2>Identity Gap</h2>
<div class=grid>
 <div class=kpi><b>{{ g.clients_measured }}</b><span>замерено клиенток</span></div>
 <div class=kpi><b>{{ g.avg_first_gap if g.avg_first_gap is not none else '—' }}%</b><span>средний Gap (старт)</span></div>
 <div class=kpi><b>{{ g.clients_with_progress }}</b><span>с повторным замером</span></div>
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
    email = session.get("email")
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
        return {"img": render_look_on_client(str(photo_path), lk.get("image_generation_prompt", "")),
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


# капсула по одежде: раскладываем вещи по слотам гардероба для наглядного борда
_CAPSULE_SLOTS = [
    ("Верхний слой", ("пальто", "тренч", "жакет", "пиджак", "куртка", "косуха", "кардиган",
                       "плащ", "шуба", "дублёнка", "бомбер", "джинсовк")),
    ("Платья и комбинезоны", ("платье", "комбинезон", "сарафан")),
    ("Верх", ("рубашка", "блуз", "топ", "футболк", "водолазк", "свитер", "джемпер", "худи",
              "свитшот", "боди", "лонгслив", "майка", "поло", "тельняшк", "корсет", "бюстье", "кроп")),
    ("Низ", ("брюки", "джинс", "юбка", "шорты", "палаццо", "легинс", "чинос", "кюлот")),
    ("Обувь", ("туфли", "лодочки", "ботинки", "ботильон", "челси", "сапог", "кроссовк", "кед",
               "босоножк", "лофер", "балетк", "сандал", "мюли", "слипон", "угги")),
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
        for slot, keys in _CAPSULE_SLOTS:
            if any(k in n for k in keys):
                return slot
    return _SLOT_OTHER


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
            "items": d.get("items") or [],
            "img": render_look_on_client(str(photo_path), _look_prompt(d, diag, season)),
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
         "items": sils[:3] or ["мягкий жакет", "прямые брюки", "блуза"],
         "image_generation_prompt": f"Спокойный образ по формуле «{formula}», мягкие чистые линии."},
        {"name": "Собранная версия", "fits_if": "Подходит, если хочется уверенности и статуса.",
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
    client = (request.form.get("client") or "").strip()
    account_email = session.get("email")
    season = (request.form.get("season") or "").strip() or None
    threading.Thread(target=_job_worker,
                     args=(job_id, photo_path, quiz, client, account_email, season),
                     daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.get("/api/result/<job_id>")
def api_result(job_id):
    """Статус/результат фоновой генерации (без внутреннего diag — он только на сервере)."""
    j = _JOBS.get(job_id) or {"status": "unknown"}
    return jsonify({k: v for k, v in j.items() if k != "diag"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "calls_today": count_today(), "limit": DEMO_DAILY_LIMIT}


if __name__ == "__main__":
    # порт 80 — этого ждёт Amvera; в проде запускает gunicorn (см. amvera.yml), не эту строку
    app.run(host="0.0.0.0", port=80, debug=False)
