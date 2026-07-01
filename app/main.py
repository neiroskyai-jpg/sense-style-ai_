"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import concurrent.futures
import io
import json
import os
import re
import threading
import uuid
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
                           refine_colortype_subtype, render_look_on_client)
from core.tracking import (chat_log, count_generations, count_today, feedback_list,
                           funnel, gap_summary, leads, progress, record_call,
                           record_chat, record_consent, record_event, record_feedback,
                           record_session)
from core.auth import make_token, read_token, send_magic_link
from core.figure_rules import fit_rules_client
from core.chat import stylist_reply
from core.profiles import (get_profile, save_card, save_diagnosis,
                           save_style_profile)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # дизайнерский сайт (статика)
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим (контроль стоимости/времени)
DEMO_DAILY_LIMIT = int(os.getenv("DEMO_DAILY_LIMIT", "40"))  # защита от слива ключа

# статика сайта раздаётся из web/ в корне; зарегистрированные роуты (/demo, /api…) важнее
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # лимит загрузки 15 МБ
# секрет сессий/magic-link: env SENSE_SECRET_KEY или стабильный файл на постоянном томе
from core.config import secret_key as _secret_key  # noqa: E402
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
 <div class=look><img src="{{ lk.img }}"><p class=desc>{{ lk.desc }}</p></div>
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
<h2>5. Хранение и обработка</h2><p>Данные граждан РФ хранятся в базах на территории РФ (ст. 18 ч. 5 152-ФЗ). Фотография обрабатывается эфемерно и не сохраняется после обработки; в базе остаются результаты и история Identity Gap. Передача по HTTPS, доступ ограничен, факт согласия журналируется.</p>
<h2>6. Передача третьим лицам</h2><p>Для генерации образов привлекается AI-обработчик (Google, Gemini); при оплате — платёжный провайдер. Данные не используются для обучения сторонних моделей и не передаются третьим лицам в их интересах.</p>
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
  <label>Email</label><input type=email name=email required placeholder="anna@example.com" value="{{ email or '' }}">
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
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/logout">Выйти</a></div>
<h1>Мой профиль</h1>
<p class=email>{{ email }}</p>
<div class=card><h3>Формула стиля {% if has_diag %}<span class="badge yes">есть</span>{% else %}<span class="badge no">ещё нет</span>{% endif %}</h3>
 <p>{% if has_diag %}{{ formula }}{% else %}Пройди диагностику — Формула сохранится здесь.{% endif %}</p></div>
<div class=card><h3>Профиль «Примерочной» {% if has_style %}<span class="badge yes">заполнен</span>{% else %}<span class="badge no">не заполнен</span>{% endif %}</h3>
 <p>Линии, ДНК стиля и анти-гардероб — чтобы проверка вещей работала мгновенно.</p></div>
<div class=links>
 {% if has_diag %}<a class=btn href="/card">Открыть Карту стиля</a>{% else %}<a class=btn href="/identity-scan-quiz.html?fresh=1">Пройти диагностику</a>{% endif %}
 <a class="btn sec" href="/garment">Проверить вещь</a>
 <a class="btn sec" href="/stylist">Спросить стилиста</a>
</div>
</div></body></html>"""


STYLE_CARD = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Карта стиля{% if name %} — {{ name }}{% endif %}</title>
<style>
 :root{--cream:#F5EFE3;--ink:#1f1d1b;--wine:#5D2230;--muted:#6b645c;--line:#e3dccf}
 *{box-sizing:border-box} body{font-family:Georgia,serif;margin:0;background:var(--cream);color:var(--ink);line-height:1.55}
 .wrap{max-width:760px;margin:0 auto;padding:30px 24px 80px}
 .bar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
 .bar a,.bar button{color:var(--wine);font:inherit;font-size:14px;background:none;border:0;cursor:pointer;text-decoration:none}
 .eyebrow{font-family:Arial,sans-serif;font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--wine)}
 h1{font-weight:normal;font-size:38px;margin:6px 0 2px} .who{color:var(--muted);margin:0 0 6px}
 h2{font-weight:normal;font-size:22px;margin:34px 0 12px;border-bottom:1px solid var(--line);padding-bottom:6px}
 .formula{font-size:22px;margin:2px 0} .gap{color:var(--wine);font-weight:bold}
 .dna{font-size:16px;line-height:1.65}
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
 .look .nm{font-size:18px;margin:3px 0 8px}
 .look .it{font-size:13.5px;color:#4a443c;margin:0 0 8px}
 .look .ds{font-size:14px;color:#5a5246}
 .cap-h{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--wine);margin:18px 0 8px;font-weight:normal}
 .caps{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}
 @media(max-width:560px){.caps{grid-template-columns:1fr}}
 .capitem{display:flex;gap:10px;align-items:flex-start;background:#fff;border:1px solid var(--line);border-radius:12px;padding:11px 13px}
 .capdot{flex:none;width:16px;height:16px;border-radius:50%;border:1px solid rgba(0,0,0,.12);margin-top:3px}
 .capitem b{font-size:14.5px;font-weight:normal} .captag{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#fff;background:var(--wine);border-radius:6px;padding:1px 6px;vertical-align:middle}
 .capwhy{font-size:12.5px;color:#7a7064}
 .capslot{margin:18px 0 8px} .capslotname{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--wine)}
 .shop{display:flex;flex-direction:column;gap:10px} .shopitem{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 16px}
 .shopname{font-size:16px;color:#2a2620} .shopwhy{font-size:13.5px;color:#5a5246;margin:3px 0 6px}
 .shoplinks{font-size:13px;color:#9a8f80} .shoplinks a{color:var(--wine);text-decoration:none}
 .ref{background:#fbf8f3;border:1px solid var(--line);border-radius:14px;padding:16px 20px}
 .refname{font-size:20px;color:var(--wine)} .refline{font-size:14px;color:#5a5246;margin:6px 0 0}
 .print{display:block;margin:30px auto 0;background:var(--wine);color:#fff;border:0;border-radius:10px;padding:14px 26px;font:inherit;font-size:16px;cursor:pointer}
 @media print{.bar,.print{display:none} body{background:#fff} .wrap{max-width:none;padding:0} .look{break-inside:avoid}}
</style></head><body><div class=wrap>
<div class=bar><a href="/me">← мой профиль</a><a href="/card?rebuild=1">пересобрать (с анкетой)</a></div>

<div class=eyebrow>Карта стиля</div>
<h1>Твоя Формула</h1>
{% if name %}<p class=who>для {{ name }}</p>{% endif %}
<p class=formula><b>{{ c.formula }}</b></p>
{% if c.gap is not none %}<p>Identity Gap: <span class=gap>{{ c.gap }}%</span> — разрыв между тем, как тебя считывают и как ты хочешь.</p>{% endif %}
{% if c.dna %}<p class=dna>{{ c.dna }}</p>{% endif %}

{% if c.colortype or c.figure %}<h2>Твоя основа</h2>
<ul class=clean>
 {% if c.colortype %}<li><b>Цветотип:</b> {{ c.colortype }}{% if c.contrast %} · контраст {{ c.contrast }}{% endif %} — на нём строится палитра ниже</li>{% endif %}
 {% if c.figure %}<li><b>Фигура:</b> {{ c.figure }} — под неё силуэты и образы</li>{% endif %}
 {% if c.emphasize %}<li><b>Подчёркиваем:</b> {{ c.emphasize }} — образы строим вокруг этого</li>{% endif %}
</ul>{% endif %}

{% if c.figure_fit %}<h2>Посадка под твою фигуру</h2>
<p style="font-size:15px;color:var(--muted);margin:0 0 10px">По этим правилам подобрана капсула и образы ниже — чтобы вещи сидели по твоим пропорциям.</p>
<ul class=clean>
 <li><b>Подчёркиваем:</b> {{ c.figure_fit.emphasize }}</li>
 <li><b>Баланс:</b> {{ c.figure_fit.balance }}</li>
 <li><b>Посадка и размеры:</b> {{ c.figure_fit.fit }}</li>
 <li><b>Твои силуэты:</b> {{ c.figure_fit.silhouettes | join('; ') }}</li>
</ul>{% endif %}

{% if c.personality and c.personality.portrait %}<h2>Твоя натура и стиль</h2>
<p style="font-size:16px;color:#3a352e;margin:0 0 12px">{{ c.personality.portrait }}</p>
{% if c.personality.style_implications %}<ul class=clean>{% for s in c.personality.style_implications %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}{% endif %}

<h2>Твоя палитра — 30 цветов</h2>
{% for grp, title in [('base','База и нейтрали'),('main','Основные'),('accent','Акценты')] %}
 {% set items = c.palette|selectattr('group','equalto',grp)|list %}
 {% if items %}<div class=sw-group>{{ title }}</div><div class=swatches>
  {% for p in items %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm>{{ p.name }}</div></div>{% endfor %}
 </div>{% endif %}
{% endfor %}

{% if c.stop_colors %}<h2>Стоп-цвета — что тебя гасит</h2>
<div class="swatches stopcolors">
 {% for p in c.stop_colors %}<div class=sw><div class=chip style="background:{{ p.hex }}"></div><div class=nm><b>{{ p.name }}</b><br>{{ p.why }}</div></div>{% endfor %}
</div>{% endif %}

{% if c.silhouettes %}<h2>Силуэты под твою фигуру</h2>
<ul class=clean>{% for s in c.silhouettes %}<li>{{ s }}</li>{% endfor %}</ul>{% endif %}

{% if c.base_capsule %}<h2>Базовая капсула — ядро гардероба</h2>
<p class=meta>Эти вещи — основа, всё остальное собирается вокруг них{% if c.combination_count %}: из них получается около {{ c.combination_count }} рабочих образов{% endif %}.</p>
{% if c.capsule_board %}
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

{% macro lookcard(lk) %}<div class=look>
  {% if lk.img %}<img src="{{ lk.img }}" alt="Образ" style="width:100%;border-radius:10px;margin-bottom:10px;display:block">{% endif %}
  {% if lk.scenario %}<div class=scn>{{ lk.scenario }}</div>{% endif %}
  {% if lk.title %}<div class=nm>{{ lk.title }}</div>{% elif lk.name %}<div class=nm>{{ lk.name }}</div>{% endif %}
  {% if lk['items'] %}<p class=it>{{ lk['items']|join(' · ') }}</p>{% endif %}
  {% if lk.description %}<p class=ds>{{ lk.description }}</p>{% endif %}
 </div>{% endmacro %}

{% if c.looks %}<h2>Капсулы под реальную жизнь</h2>
<p class=meta>Образы сгруппированы по жизни — видно, как одни и те же базовые вещи работают в разных ситуациях.</p>
{% set ns = namespace(shown=0) %}
{% for bucket in ['Работа','Повседневное','Выход'] %}
 {% set bl = c.looks|selectattr('bucket','equalto',bucket)|list %}
 {% if bl %}{% set ns.shown = ns.shown + bl|length %}<h3 class=cap-h>{{ bucket }}</h3><div class=looks>{% for lk in bl %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}
{% endfor %}
{% if ns.shown == 0 %}<div class=looks>{% for lk in c.looks %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}{% endif %}

{% if c.styling and c.styling.looks %}<h2>Стилизация: одна вещь — два образа</h2>
<p class=meta>{% if c.styling.idea %}{{ c.styling.idea }}{% else %}Одна базовая вещь{% if c.styling.base_item %} ({{ c.styling.base_item }}){% endif %} — два разных образа.{% endif %} Так работает капсула: мало вещей, много решений.</p>
<div class=looks>{% for lk in c.styling.looks %}{{ lookcard(lk) }}{% endfor %}</div>{% endif %}

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
function downloadPdf(){
  var btn=document.getElementById('pdfbtn'); var bar=document.querySelector('.bar');
  var fb=document.getElementById('fbblock');
  btn.textContent='Готовлю файл…'; btn.disabled=true;
  if(bar) bar.style.visibility='hidden'; btn.style.visibility='hidden'; if(fb) fb.style.display='none';
  var opt={margin:[10,10,12,10], filename:'Карта-стиля.pdf', image:{type:'jpeg',quality:0.96},
    html2canvas:{scale:2,useCORS:true,backgroundColor:'#F5EFE3'},
    jsPDF:{unit:'mm',format:'a4',orientation:'portrait'},
    pagebreak:{mode:['css','legacy'],avoid:'.look'}};
  html2pdf().set(opt).from(document.querySelector('.wrap')).save().then(function(){
    if(bar) bar.style.visibility='visible'; btn.style.visibility='visible'; if(fb) fb.style.display='';
    btn.textContent='Скачать PDF к шкафу'; btn.disabled=false;
  }).catch(function(){
    if(bar) bar.style.visibility='visible'; btn.style.visibility='visible'; if(fb) fb.style.display='';
    btn.textContent='Скачать PDF к шкафу'; btn.disabled=false;
  });
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
 .file{border:1.5px dashed #cdbfa6;border-radius:10px;padding:16px;text-align:center;background:#fbf8f1}
 input[type=file]{width:100%}
 button{margin-top:26px;width:100%;padding:15px;background:var(--wine);color:#fff;border:0;border-radius:10px;font-family:inherit;font-size:17px;cursor:pointer}
 .consent{font-size:13px;color:var(--muted);display:flex;gap:8px;margin-top:14px;line-height:1.4} .consent input{width:auto;margin-top:3px}
 .hint{color:var(--muted);font-size:13px;text-align:center;margin-top:14px} .hint a{color:var(--wine)}
 .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:8px}
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/me">← мой профиль</a></div>
<div class=eyebrow>Карта стиля</div>
<h1>Покажем тебя в 6 образах</h1>
<p class=lead>Загрузи фото в полный рост — соберём твою Карту стиля и покажем тебя в 6 образах под твои сценарии. Это занимает пару минут.</p>
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
 <label>Что в твоей внешности тебе нравится больше всего — что подчеркнём?</label>
 <input type=text name=adv class=fld placeholder="например: длинные ноги, талия, плечи, шея">
 <label>Что хочешь визуально уравновесить?</label>
 <input type=text name=balance class=fld placeholder="например: сбалансировать бёдра и плечи">
 <label>Стильные табу — что точно не носишь?</label>
 <input type=text name=taboo class=fld placeholder="например: не ношу мини, красный, каблук выше 5 см">
 <label>Чьё мнение учитываем в стиле? (по желанию)</label>
 <input type=text name=audience class=fld placeholder="например: никого / партнёр / дети / коллеги">

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
    link = request.url_root.rstrip("/") + "/auth?token=" + make_token(email)
    sent = send_magic_link(email, link)
    # если почта не настроена (dev) — показываем ссылку прямо на странице, чтобы можно было войти
    return render_template_string(LOGIN_PAGE, error=None, sent=True, email=email,
                                  dev_link=None if sent else link, next=nxt or "")


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


@app.get("/me")
def me():
    email = session.get("email")
    if not email:
        return redirect("/login")
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    return render_template_string(
        ME_PAGE, email=email, has_diag=bool(diag.get("style_formula")),
        formula=diag.get("style_formula", ""),
        has_style=bool(prof.get("style_profile")),
    )


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


def build_style_card(diag: dict) -> dict:
    """Собрать продукт «Карта стиля» из Формулы: палитра 30 цветов + 6 образов + секции.
    Два текстовых вызова (палитра + капсула), без рендера картинок."""
    vf = diag.get("visual_formula") or {}
    deep = diag.get("deep_intake") or {}  # глубокая диагностика из анкеты Карты
    taboo_items = [t.strip() for t in re.split(r"[;,]", deep.get("taboo", "")) if t.strip()]
    price_segment = deep.get("budget") or "middle"  # из анкеты, иначе средний
    # Палитра и капсула — на flash (dev): надёжно и быстро. pro@final в проде отдаёт
    # finish_reason=error (нестабилен), поэтому для продукта НЕ используем (2026-06-29).
    palette = generate_card_palette(diag, mode="dev")
    scenarios = ["работа", "деловая встреча", "повседневное",
                 "событие и выход", "свидание", "путешествие"]
    gen_req = {"mode": "capsule", "capsule_type": "auto", "season": "FW 2026-2027",
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
    protos = diag.get("prototypes") or []
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
        "combination_count": (capsule.get("capsule") or {}).get("combination_count"),
        "looks": looks,
        "styling": styling,  # {base_item, idea, looks:[…x2]} — рендерятся в воркере
        "shopping": (shopping.get("shopping_items") or [])[:5],
        "budget": shopping.get("budget_estimate") or {},
        "style_reference": protos[0] if protos else None,
        # личные табу из анкеты добавляем в стоп-лист (без дублей)
        "stop_list": (vf.get("stop_list") or []) + [t for t in taboo_items if t not in (vf.get("stop_list") or [])],
        "emphasize": deep.get("adv"),  # достоинство — показываем в Карте «что подчёркиваем»
        "personality": personality,  # {portrait, style_implications} или {}
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
    deep = {k: (form.get(k) or "").strip()[:200]
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


def _card_job_worker(job_id: str, photo_path: Path, email: str) -> None:
    """Фоновая сборка карты + рендер 6 образов на клиентке. Фото удаляем после."""
    try:
        diag = (get_profile(email) or {}).get("diagnosis") or {}
        card = build_style_card(diag)
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
        job_diag = (_JOBS.get(from_job) or {}).get("diag")
        if job_diag:
            save_diagnosis(email, job_diag)
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/identity-scan-quiz.html?fresh=1")  # сначала нужна диагностика (квиз)
    card = prof.get("card") or {}
    if card and not request.args.get("rebuild") and not request.args.get("text"):
        return render_template_string(STYLE_CARD, c=card, name=email,
                                      thanks=request.args.get("fb"))
    # бесплатная генерация — один раз на email; пересборку/повтор блокируем (защита токенов)
    if (request.args.get("rebuild") or request.args.get("text")) and not _gen_allowed(email):
        if card:
            return render_template_string(STYLE_CARD, c=card, name=email, thanks=None)
        return render_template_string(CARD_BUILD_FORM, error=_GEN_LIMIT_MSG), 429
    if request.args.get("text"):  # текстовая карта без образов (синхронно)
        if not _quota_left():
            return render_template_string(CARD_BUILD_FORM, error="Лимит на сегодня исчерпан."), 429
        record_call()
        try:
            card = build_style_card(diag)
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
    diag = (get_profile(email) or {}).get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/identity-scan-quiz.html?fresh=1")
    if not _gen_allowed(email):  # бесплатная генерация — один раз на email (защита токенов)
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
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "processing"}
    threading.Thread(target=_card_job_worker, args=(job_id, photo_path, email),
                     daemon=True).start()
    return render_template_string(CARD_BUILDING, job_id=job_id)


@app.get("/card/status/<job_id>")
def card_status(job_id):
    return jsonify(_JOBS.get(job_id) or {"status": "unknown"})


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
    return redirect("/card?fb=1")


# приборная панель метрик для конкурса — доступ по email основателя ИЛИ ?key=SENSE_METRICS_KEY
_ADMIN_EMAILS = {e.strip().lower() for e in
                 os.getenv("SENSE_ADMIN_EMAILS", "neiroskyai@gmail.com").split(",") if e.strip()}


def _is_admin() -> bool:
    if (session.get("email") or "").lower() in _ADMIN_EMAILS:
        return True
    key = os.getenv("SENSE_METRICS_KEY")
    return bool(key) and request.args.get("key") == key


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
<h2>Почты клиенток ({{ leads|length }}) &nbsp;<a href="/metrics/leads.csv{{ keyq }}">скачать CSV</a></h2>
<table><tr><th>Email</th><th>Первый раз</th><th>Последний</th><th>Формула</th><th>Цветотип</th><th>Gap</th><th>Отзывов</th></tr>
{% for l in leads %}<tr><td>{{ l.email }}</td><td>{{ l.first }}</td><td>{{ l.last }}</td><td>{{ l.formula or '' }}</td><td>{{ l.colortype or '' }}</td><td>{{ l.gap if l.gap is not none else '' }}</td><td>{{ l.feedback or '' }}</td></tr>{% endfor %}
{% if not leads %}<tr><td colspan=7 style="color:#6b645c">Пока нет почт.</td></tr>{% endif %}
</table>
<h2>Отзывы и комментарии ({{ f.feedback }}{% if f.avg_rating %}, средняя {{ f.avg_rating }}★{% endif %}) &nbsp;<a href="/metrics/feedback.csv{{ keyq }}">скачать CSV</a></h2>
<table><tr><th>Когда</th><th>Клиентка</th><th>Оценка</th><th>Текст</th></tr>
{% for r in fb %}<tr><td>{{ r.ts }}</td><td>{{ r.client }}</td><td class=star>{{ r.rating or '' }}</td><td>{{ r.text or '' }}</td></tr>{% endfor %}
{% if not fb %}<tr><td colspan=4 style="color:#6b645c">Пока нет отзывов.</td></tr>{% endif %}
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
    return render_template_string(METRICS_PAGE, f=funnel(), g=gap_summary(),
                                  fb=feedback_list(), leads=leads(), chat=chat_log(), keyq=keyq)


@app.get("/metrics/leads.csv")
def metrics_leads_csv():
    if not _is_admin():
        return redirect("/login?next=/metrics")
    rows = [[l["email"], l["first"], l["last"], l["formula"], l["colortype"],
             l["figure"], l["gap"], l["sessions"], l["feedback"]] for l in leads()]
    return _csv_response(rows, ["email", "first_seen", "last_seen", "formula", "colortype",
                                "figure", "gap", "sessions", "feedback_count"], "leads.csv")


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
    ("Аксессуары", ("сумк", "ремень", "пояс", "шарф", "платок", "очки", "шляп", "берет", "кепк",
                    "серьг", "браслет", "колье", "цепочк", "часы", "перчатк", "клатч", "шопер")),
]


def _capsule_slot(name: str) -> str:
    n = (name or "").lower()
    for slot, keys in _CAPSULE_SLOTS:
        if any(k in n for k in keys):
            return slot
    return "База и прочее"


def _capsule_board(items: list) -> list:
    """Группировка вещей капсулы по слотам гардероба (для визуального борда). Порядок — как в _CAPSULE_SLOTS."""
    order = [s for s, _ in _CAPSULE_SLOTS] + ["База и прочее"]
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
