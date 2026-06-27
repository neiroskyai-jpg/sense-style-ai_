"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import io
from pathlib import Path

from flask import Flask, render_template_string, request
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, generate_capsule,
                           render_look_on_client)
from core.tracking import progress, record_session

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим (контроль стоимости/времени)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # лимит загрузки 15 МБ

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
 <select name=figure><option>rectangle</option><option>hourglass</option><option>pear</option><option>inverted_triangle</option><option>apple</option></select>
 <label>Сегмент</label><select name=price><option>middle</option><option>low</option><option>high</option><option>luxury</option></select>
 <label>Табу — что точно не наденешь (через запятую)</label><input name=taboos value="">
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
<p><a href="/">← заново</a></p>
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


def _split(s: str) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


@app.get("/")
def index():
    return render_template_string(FORM, error=None)


@app.post("/analyze")
def analyze():
    file = request.files.get("photo")
    if not file or not file.filename:
        return render_template_string(FORM, error="Загрузи фото."), 400
    if file.mimetype not in ALLOWED:
        return render_template_string(FORM, error="Формат не поддержан (нужен JPEG/PNG/WebP)."), 400

    raw = file.read()
    try:  # валидируем, что это реально изображение (защита от мусора/бомб)
        Image.open(io.BytesIO(raw)).verify()
    except (UnidentifiedImageError, OSError, ValueError):
        return render_template_string(FORM, error="Файл не похож на изображение."), 400

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    photo_path = UPLOAD_DIR / secure_filename(file.filename or "photo.jpg")
    photo_path.write_bytes(raw)

    quiz = {
        "context": {"age": request.form.get("age"), "profession": request.form.get("profession")},
        "now_traits": _split(request.form.get("now_traits")),
        "want_traits_top3": _split(request.form.get("want_traits"))[:3],
        "physical": {"height": request.form.get("height"),
                     "figure_type_self_assessed": request.form.get("figure")},
        "price_segment": request.form.get("price", "middle"),
        "taboos": _split(request.form.get("taboos")),
    }

    try:
        vision = analyze_photos([str(photo_path)], height_cm=quiz["physical"]["height"], mode="dev")
        diag = diagnose(quiz, vision, mode="dev")
        gen_req = {"mode": "capsule", "capsule_type": "auto", "season": "FW 2026-2027",
                   "scenarios": ["работа", "повседневное", "выход"], "n_looks": 6,
                   "price_segment": quiz["price_segment"], "taboos": quiz["taboos"]}
        capsule = generate_capsule(diag, gen_req, mode="dev")
        looks_src = (capsule.get("looks") or [])[:N_RENDER]
        looks = []
        for lk in looks_src:
            img = render_look_on_client(str(photo_path), lk.get("image_generation_prompt", ""))
            looks.append({"img": img, "desc": lk.get("description", "")})
    except Exception as e:  # noqa: BLE001 — показать понятную ошибку, не падать страницей 500
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
