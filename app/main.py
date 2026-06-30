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
import threading
import uuid
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template_string, request,
                   session, send_from_directory)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, evaluate_garment,
                           generate_capsule, generate_card_palette,
                           generate_directions, generate_shopping_list,
                           refine_colortype_subtype, render_look_on_client)
from core.tracking import (count_today, progress, record_call, record_consent,
                           record_session)
from core.auth import make_token, read_token, send_magic_link
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
 <p class=hint style="margin:16px 0 0">Цветотип, контраст и силуэт фигуры ИИ определит сам по фото — указывать не нужно.</p>
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
  <p>ИИ-стилист на основе психологии моды. Загрузи фото и ответь на несколько вопросов — определим твою Формулу стиля, измерим разрыв между тем, как тебя считывают сейчас и как ты хочешь, и покажем тебя в новых образах.</p>
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

<div class=eyebrow>Проверка вещи · ИИ</div>
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
 {% if has_diag %}<a class=btn href="/card">Открыть Карту стиля</a>{% else %}<a class=btn href="/demo">Пройти диагностику</a>{% endif %}
 <a class="btn sec" href="/garment">Проверить вещь</a>
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
 .shop{display:flex;flex-direction:column;gap:10px} .shopitem{background:#fff;border:1px solid var(--line);border-radius:12px;padding:13px 16px}
 .shopname{font-size:16px;color:#2a2620} .shopwhy{font-size:13.5px;color:#5a5246;margin:3px 0 6px}
 .shoplinks{font-size:13px;color:#9a8f80} .shoplinks a{color:var(--wine);text-decoration:none}
 .ref{background:#fbf8f3;border:1px solid var(--line);border-radius:14px;padding:16px 20px}
 .refname{font-size:20px;color:var(--wine)} .refline{font-size:14px;color:#5a5246;margin:6px 0 0}
 .print{display:block;margin:30px auto 0;background:var(--wine);color:#fff;border:0;border-radius:10px;padding:14px 26px;font:inherit;font-size:16px;cursor:pointer}
 @media print{.bar,.print{display:none} body{background:#fff} .wrap{max-width:none;padding:0} .look{break-inside:avoid}}
</style></head><body><div class=wrap>
<div class=bar><a href="/me">← мой профиль</a><a href="/card?refresh=1">пересобрать</a></div>

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
</ul>{% endif %}

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

{% if c.looks %}<h2>6 образов под твои сценарии</h2>
<div class=looks>
 {% for lk in c.looks %}<div class=look>
  <div class=scn>{{ lk.scenario }}</div>
  {% if lk.name %}<div class=nm>{{ lk.name }}</div>{% endif %}
  {% if lk['items'] %}<p class=it>{{ lk['items']|join(' · ') }}</p>{% endif %}
  <p class=ds>{{ lk.description }}</p>
 </div>{% endfor %}
</div>{% endif %}

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
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
<script>
function downloadPdf(){
  var btn=document.getElementById('pdfbtn'); var bar=document.querySelector('.bar');
  btn.textContent='Готовлю файл…'; btn.disabled=true;
  if(bar) bar.style.visibility='hidden'; btn.style.visibility='hidden';
  var opt={margin:[10,10,12,10], filename:'Карта-стиля.pdf', image:{type:'jpeg',quality:0.96},
    html2canvas:{scale:2,useCORS:true,backgroundColor:'#F5EFE3'},
    jsPDF:{unit:'mm',format:'a4',orientation:'portrait'},
    pagebreak:{mode:['css','legacy'],avoid:'.look'}};
  html2pdf().set(opt).from(document.querySelector('.wrap')).save().then(function(){
    if(bar) bar.style.visibility='visible'; btn.style.visibility='visible';
    btn.textContent='Скачать PDF к шкафу'; btn.disabled=false;
  }).catch(function(){
    if(bar) bar.style.visibility='visible'; btn.style.visibility='visible';
    btn.textContent='Скачать PDF к шкафу'; btn.disabled=false;
  });
}
</script>
</body></html>"""


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


@app.get("/")
def landing():
    return send_from_directory(str(WEB_DIR), "index.html")


@app.get("/demo")
def demo():
    return render_template_string(FORM, error=None)


@app.get("/privacy")
def privacy():
    return render_template_string(PRIVACY)


@app.get("/login")
def login():
    if session.get("email"):
        return redirect("/me")
    return render_template_string(LOGIN_PAGE, error=None, sent=False, email="", dev_link=None)


@app.post("/login")
def login_send():
    email = (request.form.get("email") or "").strip()
    if "@" not in email or "." not in email:
        return render_template_string(LOGIN_PAGE, error="Введи корректный email.",
                                      sent=False, email=email, dev_link=None), 400
    link = request.url_root.rstrip("/") + "/auth?token=" + make_token(email)
    sent = send_magic_link(email, link)
    # если почта не настроена (dev) — показываем ссылку прямо на странице, чтобы можно было войти
    return render_template_string(LOGIN_PAGE, error=None, sent=True, email=email,
                                  dev_link=None if sent else link)


@app.get("/auth")
def auth_verify():
    email = read_token(request.args.get("token") or "")
    if not email:
        return render_template_string(
            LOGIN_PAGE, error="Ссылка недействительна или устарела — запроси новую.",
            sent=False, email="", dev_link=None), 400
    session["email"] = email
    session.permanent = True
    return redirect("/me")


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


def build_style_card(diag: dict) -> dict:
    """Собрать продукт «Карта стиля» из Формулы: палитра 30 цветов + 6 образов + секции.
    Два текстовых вызова (палитра + капсула), без рендера картинок."""
    vf = diag.get("visual_formula") or {}
    # Палитра и капсула — на flash (dev): надёжно и быстро. pro@final в проде отдаёт
    # finish_reason=error (нестабилен), поэтому для продукта НЕ используем (2026-06-29).
    palette = generate_card_palette(diag, mode="dev")
    scenarios = ["работа", "деловая встреча", "повседневное",
                 "событие и выход", "свидание", "путешествие"]
    gen_req = {"mode": "capsule", "capsule_type": "auto", "season": "FW 2026-2027",
               "scenarios": scenarios, "n_looks": 6, "price_segment": "middle", "taboos": []}
    capsule = generate_capsule(diag, gen_req, mode="dev")
    shopping = {}
    try:  # топ покупок со ссылками — не должен ронять карту
        shopping = generate_shopping_list(diag, capsule, price_segment="middle", mode="teaser")
    except Exception:  # noqa: BLE001
        shopping = {}
    protos = diag.get("prototypes") or []
    return {
        "formula": diag.get("style_formula"),
        "gap": diag.get("gap_percentage"),
        "dna": diag.get("dna_explanation", ""),
        # основа — определяется в диагностике ДО цветов/образов (цветотип → палитра, фигура → силуэты)
        "colortype": _colortype_label(diag.get("colortype")),
        "figure": _figure_label(diag.get("figure_type")),
        "contrast": _CONTRAST_RU.get((diag.get("tonal_characteristics") or {}).get("contrast"), ""),
        "palette": palette.get("palette") or [],
        "stop_colors": palette.get("stop_colors") or [],
        "silhouettes": vf.get("silhouettes") or [],
        "looks": _ensure_n_looks(capsule.get("looks") or [], scenarios, capsule, diag),
        "shopping": (shopping.get("shopping_items") or [])[:5],
        "budget": shopping.get("budget_estimate") or {},
        "style_reference": protos[0] if protos else None,
        "stop_list": vf.get("stop_list") or [],
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
                    "items": lk.get("items"), "description": lk.get("description", "")})
    return out


@app.get("/card")
def style_card():
    """Продукт «Карта стиля» — для вошедшей клиентки с готовой Формулой. Кэшируется в профиле."""
    email = session.get("email")
    if not email:
        return redirect("/login")
    prof = get_profile(email)
    diag = prof.get("diagnosis") or {}
    if not diag.get("style_formula"):
        return redirect("/demo")  # сначала нужна диагностика
    card = prof.get("card") or {}
    if not card or request.args.get("refresh"):
        if not _quota_left():
            return render_template_string(LOGIN_PAGE, error="Демо-лимит на сегодня исчерпан.",
                                          sent=False, email=email, dev_link=None), 429
        record_call()
        try:
            card = build_style_card(diag)
            save_card(email, card)
        except Exception as e:  # noqa: BLE001
            return f"Не удалось собрать карту: {e}", 500
    return render_template_string(STYLE_CARD, c=card, name=email)


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
    "autumn_light": "Осень мягкая", "autumn_natural": "Осень натуральная",
    "autumn_contrast": "Осень контрастная", "winter_light": "Зима светлая",
    "winter_natural": "Зима натуральная", "winter_contrast": "Зима контрастная",
}


def _figure_label(code):
    return _FIGURE_LABEL.get(code, code) if code else code


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
        _JOBS[job_id] = {"status": "done", "result": {
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
    """Статус/результат фоновой генерации."""
    return jsonify(_JOBS.get(job_id) or {"status": "unknown"})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "calls_today": count_today(), "limit": DEMO_DAILY_LIMIT}


if __name__ == "__main__":
    # порт 80 — этого ждёт Amvera; в проде запускает gunicorn (см. amvera.yml), не эту строку
    app.run(host="0.0.0.0", port=80, debug=False)
