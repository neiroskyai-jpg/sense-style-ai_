"""Конфигурация: ключ, базовый URL OpenRouter, маршрутизация моделей по тирам.

Ключ берётся ТОЛЬКО из переменной окружения OPENROUTER_API_KEY (Windows env var
или .env — env var имеет приоритет). В коде ключ не хранится никогда.

Тиринг:
- dev   — дёшево, для отладки пайплайна (Gemini Flash / DeepSeek);
- final — Claude, для eval и демо (качество диагностики = ядро продукта).
Переключается переменной SENSE_MODE=dev|final.
"""
from __future__ import annotations
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватит .env, НЕ перетирая уже заданные env-переменные
except ImportError:
    pass

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

MODE = os.getenv("SENSE_MODE", "dev").lower()

# ВНИМАНИЕ: точные слаги сверить с живым каталогом https://openrouter.ai/models.
# При неверном слаге OpenRouter вернёт 404 — это и есть точка исправления.
MODELS = {
    "vision": {
        "dev": "google/gemini-2.5-flash",
        "final": "anthropic/claude-sonnet-4.6",
    },
    "text": {
        "dev": "deepseek/deepseek-chat",
        "final": "anthropic/claude-sonnet-4.6",
    },
    "image": {
        # генерация образов и капсул — основная Seedream, альт Nano Banana (см. план MVP)
        "primary": "bytedance/seedream-4",
        "alt": "google/gemini-2.5-flash-image",
    },
}


def model_for(task: str, mode: str | None = None) -> str:
    """task: 'vision' | 'text'. mode: 'dev' | 'final' (по умолчанию из SENSE_MODE)."""
    return MODELS[task][(mode or MODE)]


def api_key() -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY не найден. Задай Windows-переменную окружения "
            "и перезапусти VS Code, либо добавь строку в .env."
        )
    return key
