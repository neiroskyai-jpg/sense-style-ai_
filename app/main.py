"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import concurrent.futures
import io
import os
import threading
import uuid
from pathlib import Path

from flask import (Flask, jsonify, render_template_string, request,
                   send_from_directory)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, evaluate_garment,
                           generate_capsule, generate_directions,
                           render_look_on_client)
from core.tracking import (count_today, progress, record_call, record_consent,
                           record_session)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # дизайнерский сайт (статика)
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим (контроль стоимости/времени)
DEMO_DAILY_LIMIT = int(os.getenv("DEMO_DAILY_LIMIT", "40"))  # защита от слива ключа

# статика сайта раздаётся из web/ в корне; зарегистрированные роуты (/demo, /api…) важнее
app = Flask(__name__, static_folder=str(WEB_DIR), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # лимит загрузки 15 МБ


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
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>
<h1>Диагностика стиля</h1>
<p class=hint>Загрузи фото в полный рост и ответь на несколько вопросов — определим Формулу стиля и покажем тебя в новых образах.</p>
{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/analyze" enctype="multipart/form-data">
 <label>Имя или email (чтобы отслеживать динамику)</label><input name=client value="" placeholder="anna@example.com">
 <label>Фото (портрет/в полный рост)</label><input type=file name=photo accept="image/*" required>
 <label>Рост, см</label><input type=number name=height value=168>
 <label>Возраст</label><input type=number name=age value=38>
 <label>Чем занимаешься</label><input name=profession value="руководитель отдела">
 <label>Как тебя считывают сейчас (через запятую)</label><input name=now_traits value="сдержанная, простая, незаметная">
 <label>Как хочешь, чтобы считывали — топ-3</label><input name=want_traits value="властная, элегантная, статусная">
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
<div class=looks>
 {% for lk in looks %}
 <div class=look><img src="{{ lk.img }}"><p class=desc>{{ lk.desc }}</p></div>
 {% endfor %}
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
</style></head><body><div class=wrap>
<div class=top><span class=logo>Чувство стиля</span><a href="/">← на главную</a></div>

<div class=eyebrow>Проверка вещи · ИИ</div>
<h1>Брать или не брать?</h1>
<p class=lead>Стоишь в примерочной и сомневаешься? Сфоткай вещь — и узнай за пару секунд, работает ли она на твой образ. Чтобы не покупать то, что потом висит с биркой.</p>

<div class=steps>
 <div class=step><b>1</b>Фото вещи</div>
 <div class=step><b>2</b>Какой образ ближе</div>
 <div class=step><b>3</b>Честный вердикт</div>
</div>

{% if error %}<p class=err>{{ error }}</p>{% endif %}
<form method=post action="/garment/check" enctype="multipart/form-data">
<div class=card>
 <label>Фото вещи</label>
 <div class=file><input type=file name=photo accept="image/*" required></div>

 <label>Какой образ тебе ближе <span class=sub>— выбери настроение</span></label>
 <select name=base_style>
  <option value="">Не уверена — оцени по фото вещи</option>
  <option value="classic">Собранная и статусная</option>
  <option value="natural">Естественная и лёгкая</option>
  <option value="romance">Женственная и мягкая</option>
  <option value="drama">Яркая и выразительная</option>
 </select>
 <p class=sub style="margin:8px 2px 0">Цветотип и фигуру определять не нужно — это ИИ читает сам на полной диагностике по фото.</p>

 <label class=consent style="font-weight:normal;margin-top:22px"><input type=checkbox name=consent_processing required> Согласна на обработку данных согласно <a href="/privacy" target="_blank" rel="noopener">Политике</a>.</label>
 <label class=consent style="font-weight:normal"><input type=checkbox name=consent_transfer required> Согласна на передачу фото в ИИ-сервис для анализа.</label>
</div>
 <button>Узнать вердикт →</button>
 <p class=hint>Анализ занимает несколько секунд. Фото не сохраняется после проверки.</p>
</form></div></body></html>"""


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
 {% if palette %}<span class=chip><span>палитра:</span> {{ palette }}</span>{% endif %}
 {% if figure %}<span class=chip><span>фигура:</span> {{ figure }}</span>{% endif %}
 {% if style %}<span class=chip><span>стиль:</span> {{ style }}</span>{% endif %}
</div>
<p class=reason>{{ reason }}</p>
{% if replace_with %}<div class=replace><b>Чем заменить:</b> {{ replace_with }}</div>{% endif %}

<a class=cta href="/garment">Проверить ещё вещь</a>
<div class=back><a href="/demo">Пройти полную диагностику стиля →</a></div>
</div></body></html>"""


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


# карта вердикта/совпадений → русские подписи, цвет и иконка
_VERDICT_RU = {"take": ("Брать", "#3b7a4b", "✓"), "replace": ("Заменить", "#b8860b", "↺"),
               "skip": ("Не брать", "#9b3030", "✕")}
_PALETTE_RU = {"base": "в базе твоей палитры", "accent": "акцент твоей палитры",
               "neutral": "нейтрально", "taboo": "стоп-цвет", "unclear": "не считывается"}
_FIGURE_RU = {"works": "работает на фигуру", "risky": "рискованно для фигуры",
              "wrong": "против фигуры"}
_STYLE_RU = {"core": "ядро твоего стиля", "adjacent": "смежно со стилем", "off": "вне стиля"}
_BASE_RU = {"classic": "Классика", "natural": "Натуральный",
            "romance": "Романтика", "drama": "Драма"}


def _garment_profile(form) -> dict:
    """Контекст для evaluate_garment: только желаемый образ (выбор настроения).
    Цветотип/фигуру не спрашиваем — их ИИ читает по фото на полной диагностике."""
    base = form.get("base_style") or None
    diag = {"base_style": base, "style_formula": _BASE_RU.get(base) if base else None}
    return {k: v for k, v in diag.items() if v is not None}


@app.get("/garment")
def garment():
    return render_template_string(GARMENT_FORM, error=None)


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
    record_call()
    try:
        v = evaluate_garment(str(photo_path), diag, mode="dev")
    except Exception as e:  # noqa: BLE001
        return render_template_string(GARMENT_FORM, error=f"Не удалось проверить: {e}"), 500

    verdict_ru, color, icon = _VERDICT_RU.get(v.get("verdict"), ("Спорно", "#6b645c", "?"))
    return render_template_string(
        GARMENT_RESULT, verdict_ru=verdict_ru, color=color, icon=icon,
        item=v.get("item"), reason=v.get("reason", ""),
        replace_with=v.get("replace_with"),
        palette=_PALETTE_RU.get(v.get("palette_match")),
        figure=_FIGURE_RU.get(v.get("figure_match")),
        style=_STYLE_RU.get(v.get("style_match")),
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

# Названия типов фигуры для показа клиентке — геометрия + пропорция (международная
# практика), без «фруктовых» аналогий. Внутренние коды (rectangle/pear/…) не меняем.
_FIGURE_LABEL = {
    "rectangle": "Прямоугольник — сбалансированный силуэт",
    "hourglass": "Песочные часы — выраженная талия",
    "inverted_triangle": "Перевёрнутый треугольник — объём сверху",
    "pear": "Треугольник — объём снизу",
    "apple": "Круг — объём в центре",
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


def _run_fast(photo_path: Path, quiz: dict):
    """Быстрый путь для квиза: vision → диагностика → 2 именованных направления →
    рендер параллельно. Возвращает (diag, directions, explain).

    directions[i] = {name, fits_if, items[], img}. explain — блоки объяснимости.
    """
    vision = analyze_photos([str(photo_path)], height_cm=quiz["physical"]["height"], mode="dev")
    if quiz.get("colortype_known"):
        vision["colortype"] = quiz["colortype_known"]
    diag = diagnose(quiz, vision, mode="dev")
    directions = generate_directions(diag, quiz, mode="dev")[:N_RENDER]
    if not directions:  # генерация направлений не сработала — синтезируем из диагностики
        directions = _fallback_directions(diag)

    def _render(d):
        return {
            "name": d.get("name", ""),
            "fits_if": d.get("fits_if", ""),
            "items": d.get("items") or [],
            "img": render_look_on_client(str(photo_path), _look_prompt(d, diag)),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(directions))) as ex:
        rendered = list(ex.map(_render, directions))
    return diag, rendered, _explainability(diag, quiz)


def _palette_names(diag: dict) -> str:
    pal = (diag.get("visual_formula") or {}).get("palette") or []
    names = [p.get("name", "") for p in pal if p.get("role") in ("base", "accent")][:4]
    return ", ".join(n for n in names if n)


def _look_prompt(d: dict, diag: dict) -> str:
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


def _job_worker(job_id: str, photo_path: Path, quiz: dict, client: str) -> None:
    """Фоновая генерация — чтобы HTTP-запрос не висел (таймауты/блокировка)."""
    try:
        diag, directions, explain = _run_fast(photo_path, quiz)
        if client:
            try:
                record_session(client, diag)
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
    threading.Thread(target=_job_worker, args=(job_id, photo_path, quiz, client),
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
