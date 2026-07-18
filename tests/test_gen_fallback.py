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


@pytest.fixture
def no_images_generation(monkeypatch):
    """Текст Карты строится, но все рендеры образов возвращают None."""
    store: dict = {}
    saved: list[dict] = []
    events: list[tuple] = []

    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "build_style_card", lambda diag, season=None: {
        "formula": "Soft Classic",
        "looks": [{"scenario": "деловая встреча"}, {"scenario": "свидание"}],
        "styling": {"looks": [{"scenario": "выходные"}]},
        "personality": {},
    })
    monkeypatch.setattr(m, "render_look_on_client", lambda *a, **k: None)
    monkeypatch.setattr(m, "save_card", lambda e, c: saved.append(c))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: events.append((a, k)))
    m._JOBS.clear()
    return store, saved, events


def test_no_images_without_existing_card_returns_retry_and_keeps_free_attempt(no_images_generation):
    store, saved, events = no_images_generation
    store[EMAIL] = {"diagnosis": {"style_formula": "Soft Classic"}}

    job = _run_worker("job-retry")

    assert job.get("status") == "retry"
    assert "не списана" in (job.get("error") or "")
    assert len(saved) == 1, "текстовая Карта должна сохраниться как безопасный фолбэк"
    names = [args[0] for args, _kwargs in events]
    assert "card_built" not in names, "пустая генерация не должна тратить бесплатную попытку"
    assert "card_build_no_images" in names


def test_no_images_with_existing_card_keeps_previous_card_and_returns_stale(no_images_generation):
    store, saved, events = no_images_generation
    store[EMAIL] = {"diagnosis": {"style_formula": "Soft Classic"}, "card": {"formula": "прошлая"}}

    job = _run_worker("job-no-images-stale")

    assert job.get("status") == "stale"
    assert "Карта сохранена" in (job.get("error") or "")
    assert not saved, "существующую Карту нельзя перезаписывать текстовым фолбэком без образов"
    names = [args[0] for args, _kwargs in events]
    assert "card_built" not in names
    assert "card_build_no_images" in names
