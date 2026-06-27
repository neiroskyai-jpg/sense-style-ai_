"""Sense Style AI — веб-демо: фото + квиз → диагностика и образы клиентки.

Запуск:
    python -m app.main      # http://127.0.0.1:5000

ВНИМАНИЕ: каждый сабмит реально вызывает OpenRouter (платно). Рендерим 2 образа.
"""
from __future__ import annotations
import io
import os
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from core.pipeline import (analyze_photos, diagnose, generate_capsule,
                           render_look_on_client)
from core.tracking import (count_today, progress, record_call, record_consent,
                           record_session)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "user-photos"  # в .gitignore
ALLOWED = {"image/jpeg", "image/png", "image/webp"}
N_RENDER = 2  # сколько образов рендерим (контроль стоимости/времени)
DEMO_DAILY_LIMIT = int(os.getenv("DEMO_DAILY_LIMIT", "40"))  # защита от слива ключа

app = Flask(__name__)
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
 <select name=figure><option>rectangle</option><option>hourglass</option><option>pear</option><option>inverted_triangle</option><option>apple</option></select>
 <label>Сегмент</label><select name=price><option>middle</option><option>low</option><option>high</option><option>luxury</option></select>
 <label>Табу — что точно не наденешь (через запятую)</label><input name=taboos value="">
 <label style="font-weight:normal;font-size:13px;margin-top:16px;display:flex;gap:8px"><input type=checkbox name=consent_processing required style="width:auto"> Согласна на обработку персональных данных согласно <a href="https://sense-style-site.vercel.app/privacy-policy.html" target="_blank" rel="noopener">Политике</a>.</label>
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
    }


def _run_analysis(photo_path: Path, quiz: dict) -> tuple[dict, dict, list]:
    """Сквозной анализ: vision → диагностика → капсула → рендер N образов на клиентке."""
    vision = analyze_photos([str(photo_path)], height_cm=quiz["physical"]["height"], mode="dev")
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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=False)
