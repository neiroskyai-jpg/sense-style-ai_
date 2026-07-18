"""Клиент OpenRouter (OpenAI-совместимый chat/completions).

Абстракция провайдера: модель и режим — параметры, сам провайдер сменяем за
конфиг (config.py). Если после конкурса захотим уйти на прямой Claude API —
меняется только этот модуль, пайплайн не трогаем.
"""
from __future__ import annotations
import base64
import io
import json
import os
import re
from pathlib import Path

import requests
from json_repair import repair_json
from PIL import Image

from . import config

_TIMEOUT = 120
_IMAGE_TIMEOUT = 240  # pro-рендер (Nano Banana Pro) медленнее; 120с не хватало → таймаут и фоллбэк

# защита от decompression bomb: не открываем гигантские изображения (фото от пользователя)
Image.MAX_IMAGE_PIXELS = 50_000_000  # ~50 Мп


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://sense-style.ai",
        "X-Title": "Sense Style AI",
    }


def encode_image(path: str | Path, max_side: int = 1024) -> str:
    """Сжать до max_side по длинной стороне и вернуть data-URL (base64 JPEG).

    Сжатие обязательно: экономит токены Vision и ускоряет загрузку.
    """
    img = Image.open(path).convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((round(w * scale), round(h * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.standard_b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


_YUNET_PATH = Path(__file__).resolve().parent.parent / "data" / "models" / "face_detection_yunet_2023mar.onnx"
_YUNET_READABLE: str | None = None


def _yunet_path() -> str | None:
    """Путь к модели, который сможет открыть OpenCV.

    На Windows OpenCV не читает пути с не-ASCII символами («…\\Рабочий стол\\проекты\\…») — падает
    «Can't read ONNX file». На проде (Linux) путь латиницей и проблемы нет, но локально детектор
    молча отключался бы. Поэтому при кириллице в пути один раз копируем модель во временную папку.
    """
    global _YUNET_READABLE
    if _YUNET_READABLE is not None:
        return _YUNET_READABLE or None
    if not _YUNET_PATH.exists():
        _YUNET_READABLE = ""
        return None
    p = str(_YUNET_PATH)
    try:
        p.encode("ascii")
    except UnicodeEncodeError:
        import shutil
        import tempfile
        tmp = Path(tempfile.gettempdir()) / "sense_face_yunet.onnx"
        try:
            if not tmp.exists() or tmp.stat().st_size != _YUNET_PATH.stat().st_size:
                shutil.copy2(_YUNET_PATH, tmp)
            p = str(tmp)
            p.encode("ascii")   # temp тоже может оказаться кириллическим — тогда сдаёмся
        except (OSError, UnicodeEncodeError):
            _YUNET_READABLE = ""
            return None
    _YUNET_READABLE = p
    return p


def _detect_face(img: Image.Image) -> tuple[int, int, int, int] | None:
    """Рамка лица (x, y, w, h) детектором YuNet или None.

    Haar-каскады тут не годятся: на фото клиентки у здания они находят «лица» в барельефах и
    окнах (8 срабатываний на одном кадре). YuNet — DNN, даёт уверенность: на тех же фото ровно
    одно лицо со score 0.93. OpenCV/модель опциональны — без них честно возвращаем None.
    """
    model = _yunet_path()
    if not model:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:  # noqa: BLE001 — среда без opencv: кроп деградирует до эвристики
        return None
    try:
        arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        h, w = arr.shape[:2]
        det = cv2.FaceDetectorYN.create(model, "", (w, h), score_threshold=0.6)
        _, faces = det.detect(arr)
        if faces is None or not len(faces):
            return None
        # самое крупное лицо — если в кадр попала подруга/прохожий, клиентка обычно ближе
        x, y, fw, fh = (int(v) for v in max(faces, key=lambda f: f[2] * f[3])[:4])
        return x, y, fw, fh
    except Exception:  # noqa: BLE001 — детекция не должна ронять рендер
        return None


def head_crop(path: str | Path, max_side: int = 1024) -> str | None:
    """Кроп головы/плеч крупным планом — второй референс личности для identity-preserving рендера.

    Зачем: на ростовом фото лицо ~100–150px, модель «додумывает» чужое — клиентки жалуются, что
    лицо не их. Даём отдельный кадр, где лицо занимает бОльшую часть.

    Раньше кроп брал верхнюю полосу ВО ВСЮ ШИРИНУ и апскейлил её по длинной стороне — то есть
    лицо оставалось теми же ~100px посреди неба и фасада, а «крупный кадр» был фикцией. Теперь
    находим лицо детектором и кадрируем вокруг него; без детекции — старая эвристика как фолбэк.
    """
    try:
        img = Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001 — кроп не должен ронять рендер
        return None
    w, h = img.size
    face = _detect_face(img)
    if face:
        fx, fy, fw, fh = face
        # рамка головы с плечами: шире лица втрое, выше — с запасом на волосы, ниже — на плечи
        cx, cy = fx + fw / 2, fy + fh / 2
        half_w = fw * 1.6
        top = cy - fh * 1.6
        bottom = cy + fh * 2.0
        box = (max(0, round(cx - half_w)), max(0, round(top)),
               min(w, round(cx + half_w)), min(h, round(bottom)))
        crop = img.crop(box)
    else:
        # фолбэк: доля высоты, где ожидаем голову+плечи (ростовое/3-4 → верх)
        frac = 0.45 if h >= 1.25 * w else 0.72
        crop = img.crop((0, 0, w, round(h * frac)))
    cw, ch = crop.size
    if not cw or not ch:
        return None
    scale = max(1.0, max_side / max(cw, ch))  # апскейлим мелкое лицо, не уменьшаем
    if scale != 1.0:
        crop = crop.resize((round(cw * scale), round(ch * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=92)
    return "data:image/jpeg;base64," + base64.standard_b64encode(buf.getvalue()).decode()


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(src: str | Path) -> dict:
    """src — путь к файлу ИЛИ готовый data-URL (напр. выход предыдущей генерации)."""
    url = src if isinstance(src, str) and src.startswith("data:") else encode_image(src)
    return {"type": "image_url", "image_url": {"url": url}}


def chat(model: str, system: str, content, max_tokens: int = 2048,
         json_mode: bool = True) -> str:
    """content — строка или список блоков (text_block/image_block). Вернёт текст ответа."""
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    r = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers=_headers(), json=body, timeout=_TIMEOUT,
    )
    if r.status_code >= 400:
        # понятная ошибка вместо сырого трейсбэка (частые случаи: 401 ключ, 402 баланс, 404 слаг)
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    choice = r.json()["choices"][0]
    content = choice["message"].get("content")
    if not content or not content.strip():
        # пустой ответ модели — даём диагностируемую ошибку, а не криптичный JSONDecodeError
        raise RuntimeError(
            f"Модель {model} вернула пустой ответ "
            f"(finish_reason={choice.get('finish_reason')}). "
            "Возможные причины: лимит токенов, фильтр, неподходящая модель."
        )
    return content


def chat_messages(model: str, messages: list[dict], max_tokens: int = 700) -> str:
    """Многоходовой диалог: messages = [{role, content}, …] (system + история). Вернёт текст."""
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    r = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers=_headers(), json=body, timeout=_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    choice = r.json()["choices"][0]
    content = choice["message"].get("content")
    if not content or not content.strip():
        raise RuntimeError(f"Модель {model} вернула пустой ответ (finish_reason={choice.get('finish_reason')}).")
    return content


_IMAGE_MAX_TOKENS = int(os.getenv("SENSE_IMAGE_MAX_TOKENS", "12000"))


def generate_image(prompt: str, model: str | None = None, ref_images=None) -> list[str]:
    """Генерация изображения через OpenRouter (Seedream / Nano Banana).

    ref_images — опциональные референсы (мульти-референс Seedream): фото вещи,
    палитра и т.п. Возвращает список data-URL сгенерированных картинок.
    """
    model = model or config.MODELS["image"]["primary"]
    content = [text_block(prompt)]
    if ref_images:
        content += [image_block(p) for p in ref_images]
    body = {
        "model": model,
        "modalities": ["image", "text"],
        # Без явного лимита OpenRouter резервирует максимум модели (у gemini-3-pro-image — 32768)
        # и отклоняет запрос с 402, если на ключе меньше кредитов, ХОТЯ картинке нужно много
        # меньше: изображение ~1300 токенов плюс короткий текст. Из-за этого генерация образов
        # молча падала в фолбэк «Карта без образов» при живом балансе.
        "max_tokens": _IMAGE_MAX_TOKENS,
        "messages": [{"role": "user", "content": content}],
    }
    r = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers=_headers(), json=body, timeout=_IMAGE_TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    msg = r.json()["choices"][0]["message"]
    images = msg.get("images") or []
    urls = [img["image_url"]["url"] for img in images if img.get("image_url", {}).get("url")]
    if not urls:
        refusal = msg.get("refusal")
        extra = f" Отказ модели: {refusal}" if refusal else ""
        raise RuntimeError(
            f"Модель {model} не вернула изображений (ключи message: {list(msg.keys())}).{extra}"
        )
    return urls


def save_data_url(data_url: str, path: str | Path) -> Path:
    """Сохранить data-URL (base64) картинку в файл."""
    head, _, b64 = data_url.partition(",")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.standard_b64decode(b64))
    return path


def chat_json(model: str, system: str, content, max_tokens: int = 2048,
              retries: int = 2) -> dict:
    """Запрос с гарантированным JSON. LLM изредка отдаёт битый JSON — поэтому
    ретраим вызов, а на последней попытке чиним через json_repair."""
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        raw = chat(model, system, content, max_tokens=max_tokens, json_mode=True)
        try:
            return _parse_json(raw, repair=(attempt == retries))
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
    raise RuntimeError(f"Не удалось получить валидный JSON от {model}: {last_err}")


def _parse_json(raw: str, repair: bool = False) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):  # снять markdown-обёртку ```json ... ```
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # вытащить первый {...} блок (модель могла добавить текст вокруг)
    start, end = raw.find("{"), raw.rfind("}")
    candidate = raw[start:end + 1] if start != -1 and end != -1 else raw
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        if repair:  # последняя попытка — починить битый JSON
            return json.loads(repair_json(candidate))
        raise
