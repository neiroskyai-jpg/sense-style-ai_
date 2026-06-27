"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import io
import os
from pathlib import Path

from flask import (Flask, jsonify, render_template_string, request,
                   send_from_directory)
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, generate_capsule,
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
 body{font-family:Georgia,serif;max-width:680px;margin:40px auto;padding:0 20px;color:#2b2b2b;line-height:1.5}
 h1{font-weight:normal;letter-spacing:.5px} label{display:block;margin:14px 0 4px;font-size:14px;color:#555}
 input,select,textarea{width:100%;padding:9px;border:1px solid #cfcfcf;border-radius:4px;font:inherit;box-sizing:border-box}
 button{margin-top:22px;padding:12px 22px;background:#2b2b2b;color:#fff;border:0;border-radius:4px;font:inherit;cursor:pointer}
 .hint{color:#888;font-size:13px} .err{color:#9b1c1c;background:#fdeaea;padding:12px;border-radius:4px}
</style></head><body>
<h1>Sense Style AI</h1>
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
 <label>Тип фигуры (самооценка)</label>
 <select name=figure>
  <option value="rectangle">Прямоугольник</option>
  <option value="hourglass">Песочные часы</option>
  <option value="pear">Груша</option>
  <option value="inverted_triangle">Перевёрнутый треугольник</option>
  <option value="apple">Яблоко</option>
 </select>
 <label>Цветотип (если знаешь — выбери; иначе определит ИИ по фото)</label>
 <select name=colortype_known>
  <option value="">Определить по фото (ИИ)</option>
  <option value="spring_light">Весна светлая</option>
  <option value="spring_natural">Весна натуральная</option>
  <option value="spring_contrast">Весна контрастная</option>
  <option value="summer_light">Лето светлое</option>
  <option value="summer_natural">Лето натуральное</option>
  <option value="summer_contrast">Лето контрастное</option>
  <option value="autumn_light">Осень мягкая</option>
  <option value="autumn_natural">Осень натуральная</option>
  <option value="autumn_contrast">Осень контрастная</option>
  <option value="winter_light">Зима светлая</option>
  <option value="winter_natural">Зима натуральная</option>
  <option value="winter_contrast">Зима контрастная</option>
 </select>
 <label>Сегмент бюджета</label>
 <select name=price>
  <option value="middle">Средний</option>
  <option value="low">Масс-маркет</option>
  <option value="high">Премиум</option>
  <option value="luxury">Люкс</option>
 </select>
 <label>Табу — что точно не наденешь (через запятую)</label><input name=taboos value="">
 <label style="font-weight:normal;font-size:13px;margin-top:16px;display:flex;gap:8px"><input type=checkbox name=consent_processing required style="width:auto"> Согласна на обработку персональных данных согласно <a href="/privacy" target="_blank" rel="noopener">Политике</a>.</label>
 <label style="font-weight:normal;font-size:13px;display:flex;gap:8px"><input type=checkbox name=consent_transfer required style="width:auto"> Согласна на трансграничную передачу фото в AI-сервисы (Google, США) для генерации образов.</label>
 <button>Построить образы</button>
 <p class=hint>Обработка занимает ~1–2 минуты: анализ фото, диагностика, генерация образов.</p>
</form></body></html>"""

RESULT = """<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1"><title>Твоя Формула стиля</title>
<style>
 body{font-family:Georgia,serif;max-width:920px;margin:40px auto;padding:0 20px;color:#2b2b2b;line-height:1.55}
 h1,h2{font-weight:normal} .gap{font-size:42px} .formula{font-size:22px;color:#1f1f1f}
 .looks{display:flex;gap:18px;flex-wrap:wrap;margin-top:18px}
 .look{flex:1 1 260px} .look img{width:100%;border-radius:6px} .desc{font-size:14px;color:#444}
 .meta{color:#666;font-size:14px} a{color:#2b2b2b}
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
<h2>1. Оператор</h2><p>ИП Колупаева Ксения Викторовна (Санкт-Петербург, РФ). Контакт: sense-style.ru@yandex.ru.</p>
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
               "scenarios": ["работа", "повседневное", "выход"], "n_looks": 6,
               "price_segment": quiz["price_segment"], "taboos": quiz["taboos"]}
    capsule = generate_capsule(diag, gen_req, mode="dev")
    looks = []
    for lk in (capsule.get("looks") or [])[:N_RENDER]:
        img = render_look_on_client(str(photo_path), lk.get("image_generation_prompt", ""))
        looks.append({"img": img, "desc": lk.get("description", "")})
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
        dna=diag.get("dna_explanation", ""), colortype=diag.get("colortype"),
        figure=diag.get("figure_type"), items=len(cap.get("items") or []), looks=looks,
        prog=prog,
    )


@app.post("/api/analyze")
def api_analyze():
    """JSON-API для интеграции внешнего квиза (Vercel). Возвращает диагностику + образы."""
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
    try:
        diag, capsule, looks = _run_analysis(photo_path, quiz)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500

    client = (request.form.get("client") or "").strip()
    if client:
        record_session(client, diag)

    cap = capsule.get("capsule") or {}
    return jsonify({
        "gap_percentage": diag.get("gap_percentage"),
        "style_formula": diag.get("style_formula"),
        "dna_explanation": diag.get("dna_explanation"),
        "colortype": diag.get("colortype"),
        "figure_type": diag.get("figure_type"),
        "items_count": len(cap.get("items") or []),
        "looks": looks,  # [{img: data-url, desc}]
    })


@app.get("/healthz")
def healthz():
    return {"status": "ok", "calls_today": count_today(), "limit": DEMO_DAILY_LIMIT}


if __name__ == "__main__":
    # порт 80 — этого ждёт Amvera; в проде запускает gunicorn (см. amvera.yml), не эту строку
    app.run(host="0.0.0.0", port=80, debug=False)
