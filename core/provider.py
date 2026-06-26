"""Клиент OpenRouter (OpenAI-совместимый chat/completions).

Абстракция провайдера: модель и режим — параметры, сам провайдер сменяем за
конфиг (config.py). Если после конкурса захотим уйти на прямой Claude API —
меняется только этот модуль, пайплайн не трогаем.
"""
from __future__ import annotations
import base64
import io
import json
from pathlib import Path

import requests
from PIL import Image

from . import config

_TIMEOUT = 120


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


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(path: str | Path) -> dict:
    return {"type": "image_url", "image_url": {"url": encode_image(path)}}


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


def chat_json(model: str, system: str, content, max_tokens: int = 2048) -> dict:
    return _parse_json(chat(model, system, content, max_tokens=max_tokens, json_mode=True))


def _parse_json(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # модель обернула JSON в текст/markdown — вытащить первый {...} блок
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            return json.loads(raw[start:end + 1])
        raise
