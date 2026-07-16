"""Устойчивость живой генерации (Фаза 7 плана кабинета): демо на сцене не должно выглядеть падением.

Проверяет две вещи, которые ломали впечатление от продукта:
1. Наружу не уходит сырой ответ провайдера («OpenRouter 402: {"error":…Insufficient credits}») —
   клиентке он ничего не говорит, а на защите читается как упавший продукт.
2. Если готовая Карта уже есть, отказ генерации даёт `stale` (предложим открыть прошлую Карту),
   а не `error` в лоб. Без Карты — честный `error`.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

EMAIL = "fallback@test.ru"
_402 = 'OpenRouter 402: {"error":{"message":"Insufficient credits. Add more using https://openrouter.ai/credits"}}'


@pytest.mark.parametrize("exc, expect_word", [
    (RuntimeError(_402), "лимит"),
    (RuntimeError("HTTPSConnectionPool(host='openrouter.ai', port=443): Read timed out."), "дольше"),
    (RuntimeError('OpenRouter 401: {"error":{"message":"No auth credentials found"}}'), "провайдер"),
    (ValueError("неожиданное"), "завершилась"),
])
def test_friendly_error_hides_provider_internals(exc, expect_word):
    out = m._friendly_gen_error(exc)
    assert expect_word in out.lower()
    for leak in ("{", "openrouter.ai", "HTTPSConnectionPool", "402", "401", "credits"):
        assert leak not in out, f"наружу утёк сырой текст провайдера: {out}"


@pytest.fixture
def broken_generation(monkeypatch):
    """Генерация падает на 402; профиль подменяем словарём."""
    store: dict = {}

    def boom(*a, **k):
        raise RuntimeError(_402)

    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e))
    monkeypatch.setattr(m, "build_style_card", boom)
    monkeypatch.setattr(m, "save_card", lambda e, c: None)
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    m._JOBS.clear()
    return store


def _run_worker(job_id: str) -> dict:
    """Прогнать воркер на временном фото, вернуть запись задачи."""
    fd, photo = tempfile.mkstemp(suffix=".jpg")
    os.write(fd, b"x")
    os.close(fd)
    m._card_job_worker(job_id, Path(photo), EMAIL, "fw")
    assert not os.path.exists(photo), "фото обязано удаляться даже при ошибке (Политика)"
    return m._JOBS.get(job_id) or {}


def test_existing_card_gives_stale_not_error(broken_generation):
    broken_generation[EMAIL] = {"card": {"formula": "прошлая"}, "diagnosis": {}}
    job = _run_worker("job-stale")
    assert job.get("status") == "stale"          # предложим открыть прошлую Карту
    assert "лимит" in (job.get("error") or "")


def test_without_card_gives_honest_error(broken_generation):
    broken_generation[EMAIL] = {"diagnosis": {}}
    job = _run_worker("job-error")
    assert job.get("status") == "error"
    assert "{" not in (job.get("error") or "")
