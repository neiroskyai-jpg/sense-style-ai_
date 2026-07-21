"""Кеш сгенерированных образов: одна и та же картинка не генерируется дважды.

Зачем: одна Карта стиля — это 8 обращений к image-модели (6 образов + 2 стилизации), плюс 2 после
квиза. Пересборка Карты, повторный прогон демо или обновление страницы сжигали ключ заново, хотя
результат был бы тот же. Для сервиса с лимитом на ключе это главная статья расхода.

Ключ обязан включать ОТПЕЧАТОК ФОТО клиентки. Кеш по «состав образа + сезон» (как просилось в ТЗ)
без личности означал бы, что двум разным клиенткам с похожим образом вернётся одна картинка — то
есть одной покажут лицо и фигуру другой. Совпадение ключа допустимо только у человека с самим собой.

Хранилище — файлы: переживает рестарт контейнера (в отличие от памяти процесса) и не тянет
зависимостей. Картинки лежат как data-URL, поэтому отдаются в HTML напрямую.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

CACHE_DIR = Path(os.getenv("SENSE_IMG_CACHE_DIR",
                           str(Path(__file__).resolve().parent.parent / "data" / "cache" / "looks")))

# Сколько кадров держим. Каждый data-URL ~1-1.5 МБ, 400 штук ≈ 500 МБ — верх для контейнера
# Amvera. При превышении вычищаем самые старые по времени обращения.
MAX_ENTRIES = int(os.getenv("SENSE_IMG_CACHE_MAX", "400"))

# Выключатель на случай отладки генерации: с ним каждый прогон идёт в модель.
ENABLED = os.getenv("SENSE_IMG_CACHE", "1") != "0"


def _fingerprint(photo_path: str) -> str:
    """Отпечаток фото клиентки. По содержимому, а не по имени файла: одно и то же фото,
    загруженное второй раз, получает новое имя во временной папке и иначе считалось бы чужим."""
    try:
        data = Path(photo_path).read_bytes()
    except OSError:
        return "nophoto"
    return hashlib.sha256(data).hexdigest()[:16]


def make_key(photo_path: str, prompt: str, season: str | None, model: str | None = None) -> str:
    """Ключ кадра: личность + что на ней надето + сезон + модель генерации.

    Модель в ключе нужна, чтобы после смены провайдера не отдавались кадры старого качества.
    """
    raw = "|".join([_fingerprint(photo_path), (prompt or "").strip(),
                    (season or "").strip().lower(), (model or "").strip()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str) -> str | None:
    """Готовый кадр по ключу или None. Любая ошибка чтения — как промах: генерация важнее кеша."""
    if not ENABLED:
        return None
    f = CACHE_DIR / f"{key}.txt"
    try:
        if not f.exists():
            return None
        data = f.read_text(encoding="utf-8")
        os.utime(f, None)          # отметка обращения — по ней чистим самые залежавшиеся
        return data or None
    except OSError:
        return None


def put(key: str, data_url: str) -> None:
    """Сохранить кадр. Пустое значение не кешируем: иначе неудачная генерация закрепится навсегда."""
    if not ENABLED or not data_url:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (CACHE_DIR / f"{key}.txt").write_text(data_url, encoding="utf-8")
        _evict()
    except OSError:
        pass                        # кеш — ускорение, а не обязательство: молча живём без него


def _evict() -> None:
    """Держим размер в пределах MAX_ENTRIES, выбрасывая самые давно не используемые."""
    try:
        files = sorted(CACHE_DIR.glob("*.txt"), key=lambda f: f.stat().st_atime)
        for f in files[:max(0, len(files) - MAX_ENTRIES)]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def stats() -> dict:
    """Сколько кадров лежит и сколько занимают — для /healthz и метрик экономии."""
    try:
        files = list(CACHE_DIR.glob("*.txt"))
        return {"entries": len(files),
                "mb": round(sum(f.stat().st_size for f in files) / 1_048_576, 1)}
    except OSError:
        return {"entries": 0, "mb": 0.0}


def cached_render(photo_path: str, prompt: str, season: str | None, model: str | None,
                  generate) -> str:
    """Отдать кадр из кеша или сгенерировать и запомнить.

    Единственная точка входа для вызывающего кода: он не знает про ключи и файлы, просто передаёт
    функцию генерации. Сбой кеша не должен мешать генерации — поэтому все ошибки глушатся внутри
    get/put, а не здесь.
    """
    key = make_key(photo_path, prompt, season, model)
    hit = get(key)
    if hit:
        HITS["n"] += 1
        return hit
    img = generate()
    put(key, img)
    return img


# Попадания за жизнь процесса — для метрики «сэкономлено генераций» в /healthz.
HITS = {"n": 0}
