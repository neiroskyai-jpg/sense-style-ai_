"""Проверки продуктового флоу тарифов: CTA ведут в свои сценарии, а не в квиз по кругу.

Нам важно доказать две вещи:
1. В landing тарифы разведены по своим ссылкам: диагностика -> квиз, Карта -> /card, кабинет -> /cabinet.
2. Второй тариф не ломается без Карты: /cabinet отправляет в /card, а не назад в квиз.
"""
import os
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

EMAIL = "tiers@test.ru"


def test_tariffs_html_has_separate_links_for_each_step():
    html = Path("web/index.html").read_text(encoding="utf-8")
    tiers = html.split('<section id="tiers">', 1)[1].split("</section>", 1)[0]

    assert 'href="identity-scan-quiz.html"' in tiers
    # Кнопки тарифов ведут через маршрут по состоянию, а не прямо в продукт: человек с уже
    # пройденным квизом должен попадать в Карту, а не на экран «сначала диагностика».
    assert 'href="/start/card"' in tiers
    assert 'href="/start/daily"' in tiers


@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    store: dict = {}

    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "save_card", lambda e, c: store.setdefault(e, {}).__setitem__("card", c))
    monkeypatch.setattr(m, "current_card_by_season", lambda e: {"fw": store.get(e, {}).get("card")} if store.get(e, {}).get("card") else {})
    monkeypatch.setattr(m, "gap_progress", lambda e: None)
    monkeypatch.setattr(m, "_visual_capsule", lambda *a, **k: [])
    monkeypatch.setattr(m, "_capsule_board", lambda items: [{"slot": "Верх", "items": [{"name": "Жакет"}]}] if items else [])
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_call", lambda: None)
    monkeypatch.setattr(m, "_quota_left", lambda: True)
    monkeypatch.setattr(m, "_gen_allowed", lambda e: True)
    monkeypatch.setattr(m, "build_style_card", lambda diag, season=None: {
        "formula": diag.get("style_formula"), "gap": diag.get("gap_percentage"),
        "palette": [], "base_capsule": [{"name": "Жакет"}], "looks": [],
    })

    with m.app.test_client() as c:
        yield c, store


def test_card_tariff_opens_card_builder_when_diagnosis_exists(client):
    c, store = client
    store[EMAIL] = {"diagnosis": {"style_formula": "Soft Classic", "gap_percentage": 44}}

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/card")

    assert r.status_code == 200
    assert "Покажем тебя в 6 образах" in r.get_data(as_text=True)


def test_daily_tariff_without_card_routes_to_card_not_quiz(client):
    c, store = client
    store[EMAIL] = {"diagnosis": {"style_formula": "Soft Classic", "gap_percentage": 44}}

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/cabinet")

    assert r.status_code == 302
    assert r.headers["Location"].endswith("/card")


def test_daily_tariff_with_existing_card_opens_cabinet(client):
    c, store = client
    store[EMAIL] = {
        "diagnosis": {"style_formula": "Soft Classic", "gap_percentage": 44},
        "card": {
            "formula": "Soft Classic",
            "gap": 44,
            "palette": [],
            "base_capsule": [{"name": "Жакет"}],
            "looks": [{"scenario": "деловая встреча", "bucket": "Работа", "items": ["Жакет", "Брюки"]}],
            "shopping": [],
            "season": "fw",
            "season_label": "Осень-зима",
        },
    }

    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/cabinet")
    html = r.get_data(as_text=True)

    assert r.status_code == 200
    # Кабинет называется по тарифу — «Стиль каждый день», а его рабочее ядро — конструктор капсулы.
    assert "Стиль каждый день" in html
    assert "Капсульный конструктор образов" in html


# ── Кнопки тарифов ведут по состоянию пользователя ──────────────────────────────────────────
# Жалоба фаундера 19.07.2026: «захожу в тарифы — переводит на квиз, так не должно быть».
# Канон: прошла квиз → кнопка открывает Карту; собрала Карту → открывается «Стиль каждый день».

def _diagnosed(store, email):
    store[email] = {"diagnosis": {"style_formula": "Классика × Драма", "gap_percentage": 40}}


def test_tier_card_opens_card_when_quiz_is_done(client):
    """Квиз пройден — кнопка «Карта стиля» ведёт в Карту, а не на экран диагностики."""
    c, store = client
    _diagnosed(store, EMAIL)
    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/start/card")

    assert r.status_code == 302
    assert r.headers["Location"] == "/card"


def test_tier_daily_opens_cabinet_when_card_is_built(client):
    """Карта собрана — кнопка «Стиль каждый день» ведёт в кабинет."""
    c, store = client
    _diagnosed(store, EMAIL)
    store[EMAIL]["card"] = {"formula": "Классика × Драма", "season": "fw"}
    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/start/daily")

    assert r.status_code == 302
    assert r.headers["Location"] == "/cabinet"


def test_tier_daily_without_card_goes_to_card_not_quiz(client):
    """Диагностика есть, Карты нет: кабинет продолжает Карту, поэтому ведём собрать её."""
    c, store = client
    _diagnosed(store, EMAIL)
    with c.session_transaction() as s:
        s["email"] = EMAIL

    r = c.get("/start/daily")

    assert r.status_code == 302
    assert r.headers["Location"] == "/card", "не квиз: диагностика уже пройдена"


def test_tier_without_diagnosis_goes_to_quiz(client):
    """Ничего не пройдено — показывать в продукте нечего, ведём в диагностику."""
    c, _ = client

    for url in ("/start/card", "/start/daily"):
        r = c.get(url)
        assert r.status_code == 302
        assert r.headers["Location"] == "/quiz", url
