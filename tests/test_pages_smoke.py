"""Быстрый smoke-чек основных страниц перед живым прогоном.

Задача не заменить детальные тесты, а быстро подтвердить, что главные экраны конкурса
вообще открываются и тарифные переходы не развалились после визуальных правок.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

USER = "smoke@test.ru"


@pytest.fixture
def client(monkeypatch):
    store: dict = {}
    m.app.config["TESTING"] = True

    monkeypatch.setattr(m, "_current_user", lambda: USER)
    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "save_card", lambda e, c: store.setdefault(e, {}).__setitem__("card", c))
    monkeypatch.setattr(m, "current_card_by_season", lambda e: {"autumn": store.get(e, {}).get("card")} if store.get(e, {}).get("card") else {})
    monkeypatch.setattr(m, "gap_progress", lambda e: None)
    monkeypatch.setattr(m, "wardrobe_items", lambda e: [])
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_call", lambda: None)
    monkeypatch.setattr(m, "_quota_left", lambda: True)
    monkeypatch.setattr(m, "_gen_allowed", lambda e: True)
    monkeypatch.setattr(m, "_visual_capsule", lambda *a, **k: [])
    monkeypatch.setattr(m, "_capsule_board", lambda items: [{"slot": "Верх", "items": [{"name": "Жакет"}]}] if items else [])
    monkeypatch.setattr(m, "card_link_token", lambda e: "tokentesttokentest1234")

    with m.app.test_client() as c:
        yield c, store


def test_public_pages_open(client):
    c, _ = client

    assert c.get("/").status_code == 200
    assert c.get("/privacy").status_code == 200
    assert c.get("/garment").status_code == 200
    assert c.get("/me").status_code == 200


def test_quiz_short_route_redirects_to_static_quiz(client):
    c, _ = client

    r = c.get("/quiz")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/identity-scan-quiz.html?fresh=1")


def test_card_page_opens_builder_after_diagnosis(client):
    c, store = client
    store[USER] = {"diagnosis": {"style_formula": "Классика × Драма", "gap_percentage": 41}}

    r = c.get("/card")

    assert r.status_code == 200
    assert "Покажем тебя в 6 образах" in r.get_data(as_text=True)


def test_cabinet_page_opens_when_card_exists(client):
    c, store = client
    store[USER] = {
        "diagnosis": {"style_formula": "Классика × Драма", "gap_percentage": 41},
        "card": {
            "formula": "Классика × Драма",
            "gap": 41,
            "season": "autumn",
            "season_label": "Осень 2026",
            "palette": [],
            "base_capsule": [{"name": "Жакет"}],
            "capsule_board": [{"slot": "Верх", "items": [{"name": "Жакет"}]}],
            "looks": [{"scenario": "деловая встреча", "bucket": "Работа", "items": ["Жакет", "Брюки"]}],
            "shopping": [],
        },
    }

    r = c.get("/cabinet")
    html = r.get_data(as_text=True)

    assert r.status_code == 200
    assert "Стиль каждый день" in html
    assert "Капсульный конструктор образов" in html


def test_dynamic_pages_are_not_cached(client):
    c, store = client
    store[USER] = {
        "diagnosis": {"style_formula": "Классика × Драма", "gap_percentage": 41},
        "card": {"formula": "Классика × Драма", "season": "autumn"},
    }

    for url in ("/card", "/cabinet"):
        r = c.get(url)
        assert r.headers["Cache-Control"] == "no-store, no-cache, must-revalidate, max-age=0"
        assert r.headers["Pragma"] == "no-cache"
        assert r.headers["Expires"] == "0"
        assert "Cookie" in r.headers["Vary"]


def test_tariff_entry_routes_stay_working(client):
    c, store = client
    store[USER] = {"diagnosis": {"style_formula": "Классика × Драма", "gap_percentage": 41}}

    assert c.get("/start/card").headers["Location"] == "/card"
    assert c.get("/start/daily").headers["Location"] == "/card"

    store[USER]["card"] = {"formula": "Классика × Драма", "season": "autumn"}
    assert c.get("/start/daily").headers["Location"] == "/cabinet"
