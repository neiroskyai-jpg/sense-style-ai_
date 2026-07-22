"""Сквозной тест пути клиента квиз → Карта (без внешних API).

Проверяет две вещи, которые ломались на живых прогонах:
1. Gap един: число из квиза доходит до Карты без искажения — даже если сервер перезапустился
   между квизом и Картой (in-memory `_JOBS` потерян, диагноз берётся с диска). Петли на квиз нет.
2. Повторный квиз не показывает старую Карту: клиентка видит НОВЫЙ Gap и предложение пересобрать.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

EMAIL = "flow@test.ru"


def _diag(gap: int) -> dict:
    return {"gap_percentage": gap, "style_formula": "Power Woman × Smart Casual",
            "semantic_field_distribution": {"drama": 1}, "want_traits_top3": ["властная"]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    m.app.config["TESTING"] = True
    store: dict = {}
    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "save_card", lambda e, c: store.setdefault(e, {}).__setitem__("card", c))
    monkeypatch.setattr(m, "_PENDING_DIR", tmp_path / "pending")   # диск — во временную папку
    monkeypatch.setattr(m, "record_call", lambda: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "_quota_left", lambda: True)
    monkeypatch.setattr(m, "_gen_allowed", lambda e: True)
    # сборка Карты без LLM: Карта наследует Gap диагностики (как в реальном build_style_card)
    monkeypatch.setattr(m, "build_style_card", lambda diag, season=None: {
        "gap": diag.get("gap_percentage"), "formula": diag.get("style_formula"),
        "_diag_sig": m._diag_signature(diag), "palette": [],
    })
    with m.app.test_client() as c:
        yield c, store


def test_quiz_gap_reaches_card_even_after_server_restart(client):
    """Квиз дал Gap 44 → сервер перезапустился (_JOBS пуст) → Карта всё равно строится на 44.
    Раньше здесь была петля: /card не находил диагноз и редиректил обратно на квиз."""
    c, store = client
    m._save_pending_diag("job-abc", _diag(44))   # квиз сохранил диагноз на диск
    m._JOBS.clear()                              # рестарт сервера: память потеряна

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/card?from_job=job-abc&text=1")

    assert r.status_code == 200, "не должно быть редиректа на квиз (петля)"
    assert store[EMAIL]["diagnosis"]["gap_percentage"] == 44, "диагноз квиза привязан к аккаунту"
    assert store[EMAIL]["card"]["gap"] == 44, "Карта собрана на том же Gap"
    assert "44%" in r.get_data(as_text=True), "Карта показывает тот же Gap, что и квиз"


def test_repeat_quiz_shows_new_gap_not_old_card(client):
    """Прошла квиз заново (44 → 55): показываем новый Gap и предлагаем пересборку.

    Карту при этом НЕ прячем. Раньше вместо неё показывалась форма загрузки фото — со стороны
    клиентки это читалось как петля: прошла диагностику и вернулась в начало. Числа на экране
    берутся из свежей диагностики, а баннер честно говорит, что подборка ниже собрана на прежней.
    """
    c, store = client
    old = _diag(44)
    store[EMAIL] = {"diagnosis": old, "card": {"gap": 44, "_diag_sig": m._diag_signature(old)}}
    store[EMAIL]["diagnosis"] = _diag(55)        # новый прогон квиза

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/card")
    html = r.get_data(as_text=True)

    assert r.status_code == 200
    assert "55" in html, "показан новый Gap из свежего квиза"
    assert "Твоя диагностика обновилась" in html, "клиентка знает, что Карта на прежней"
    assert "Собрать Карту заново" in html, "предложена пересборка"
    assert "Покажем тебя в 6 образах" not in html, "форма загрузки фото — это возврат в начало"


def test_legacy_card_without_signature_gap_mismatch_forces_rebuild(client):
    """Старая Карта без отпечатка (собрана до фичи) и разошедшийся Gap → пересборка, не старый Gap."""
    c, store = client
    store[EMAIL] = {"diagnosis": _diag(55), "card": {"gap": 78}}  # без _diag_sig, как у ранних Карт

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/card")
    html = r.get_data(as_text=True)

    assert "55" in html, "показан актуальный Gap"
    assert "Собрать Карту заново" in html
