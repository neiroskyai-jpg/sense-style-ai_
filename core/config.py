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
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()  # подхватит .env, НЕ перетирая уже заданные env-переменные
except ImportError:
    pass

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def data_dir() -> Path:
    """Каталог данных. На Amvera — постоянный том `/data` (persistenceMount), иначе
    локальная `data/`. Переопределяется `SENSE_DATA_DIR`. Без env работает само:
    если `/data` смонтирован — используем его, чтобы данные переживали редеплой."""
    env = os.getenv("SENSE_DATA_DIR")
    if env:
        return Path(env)
    mount = Path("/data")
    return mount if mount.is_dir() else Path(__file__).resolve().parent.parent / "data"


def secret_key() -> str:
    """Секрет для подписи сессий и magic-link. Приоритет — `SENSE_SECRET_KEY`; иначе
    стабильный секрет из файла на постоянном томе (генерируется один раз, не в git),
    чтобы сессии не слетали при редеплое и секрет не лежал в репозитории."""
    env = os.getenv("SENSE_SECRET_KEY")
    if env:
        return env
    f = data_dir() / "secret_key"
    try:
        if f.exists():
            return f.read_text(encoding="utf-8").strip()
        key = secrets.token_urlsafe(48)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(key, encoding="utf-8")
        return key
    except OSError:
        return "dev-insecure-secret-change-in-prod"

MODE = os.getenv("SENSE_MODE", "dev").lower()

# ВНИМАНИЕ: точные слаги сверить с живым каталогом https://openrouter.ai/models.
# При неверном слаге OpenRouter вернёт 404 — это и есть точка исправления.
MODELS = {
    # стек на Gemini. ПРОДАКШЕН-ГЕНЕРАЦИЯ (диагностика/палитра/капсула/направления) — на
    # gemini-2.5-flash (dev): быстро (~8с) и полный JSON. gemini-2.5-pro («думающая») для
    # СТРУКТУРИРОВАННОЙ генерации НЕ годится — медленно (~90с) и обрезает JSON (проверено
    # вживую 2026-06-29). final=pro оставлен только под eval/оценку (там reasoning в плюс,
    # max_tokens большой) — НЕ использовать для генерации продукта.
    "vision": {
        "dev": "google/gemini-2.5-flash",
        "final": "google/gemini-2.5-pro",
    },
    "text": {
        "dev": "google/gemini-2.5-flash",
        "final": "google/gemini-2.5-pro",
    },
    "image": {
        # Seedream на OpenRouter недоступна → работаем на Gemini/GPT image
        "primary": "google/gemini-2.5-flash-image",   # Nano Banana — дешёвый превью/дев
        "alt": "google/gemini-3-pro-image-preview",   # для A/B качества (Nano Banana Pro)
        # identity-preserving рендер (фото клиентки → она в образе).
        # GPT image отпал: OpenAI отказывается воссоздавать реальные лица (refusal).
        # 3-pro (Nano Banana Pro) точнее держит лицо (~40-50с/образ). ВАЖНО: слаг с суффиксом -preview,
        # без него OpenRouter отдаёт 404 и генерация молча падает в фоллбэк (показ оригинала).
        "dressing": "google/gemini-3-pro-image-preview",     # точное лицо (identity-preserving)
        "dressing_hq": "google/gemini-3-pro-image-preview",
        # Раскладка вещей (flat-lay): человека в кадре НЕТ, поэтому запрет OpenAI на воссоздание
        # реальных лиц здесь не мешает — можно брать GPT-image, он силён в предметной съёмке.
        # Меняется независимо от dressing: эксперимент с раскладкой не должен задевать
        # персональные образы, где Gemini единственный держит лицо клиентки.
        # Раскладку рисует Gemini — по решению фаундера после сравнения на живых генерациях.
        # GPT-image давал чистый фон, но повторял вещи (две одинаковые пары обуви в кадре).
        # Модель меняется одной переменной и не влияет на персональные образы.
        "flatlay": os.getenv("SENSE_FLATLAY_MODEL", "google/gemini-3-pro-image-preview"),
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
