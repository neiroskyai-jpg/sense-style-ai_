"""Общие условия для всех тестов.

Главное здесь — изоляция кеша образов. Без неё тесты писали кадры в настоящий `data/cache/looks`
проекта и влияли друг на друга: тест, проверяющий содержимое промпта, получал кадр из кеша,
оставленного соседним тестом, генерация не вызывалась и проверка падала с KeyError.

Прогон тестов не должен ни оставлять мусор в рабочем кеше, ни зависеть от того, что там лежит.
"""
import os

import pytest

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")


@pytest.fixture(autouse=True)
def isolated_image_cache(tmp_path, monkeypatch):
    """Каждому тесту — свой пустой кеш образов во временной папке."""
    try:
        from core import imgcache
    except Exception:  # noqa: BLE001 — окружение без core не должно ронять сбор тестов
        return
    monkeypatch.setattr(imgcache, "CACHE_DIR", tmp_path / "imgcache", raising=False)
    monkeypatch.setattr(imgcache, "HITS", {"n": 0}, raising=False)
