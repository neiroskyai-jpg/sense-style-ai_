"""Путь без регистрации: клиентка проходит квиз → Карту → кабинет, не оставляя почты.

Барьер входа стоял на /card и /cabinet и обрывал воронку: после квиза клиентку выбрасывало
на /login, а на проде без ключей UniSender письмо не уходило вовсе — путь кончался ничем.
Личность держится в подписанной сессии (`anon-<hex>`), лимит бесплатных генераций — на ней же
плюс контур по IP (cookie можно почистить).
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

DIAG = {"style_formula": "Классика 60 / Натуральность 40", "gap_percentage": 62}


@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    store: dict = {}

    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "save_card", lambda e, c: store.setdefault(e, {}).__setitem__("card", c))
    monkeypatch.setattr(m, "current_card_by_season", lambda e: {})
    monkeypatch.setattr(m, "gap_progress", lambda e: None)
    monkeypatch.setattr(m, "_visual_capsule", lambda *a, **k: [])
    monkeypatch.setattr(m, "_capsule_board", lambda items: [])
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "_card_stale", lambda prof: False)
    c = m.app.test_client()
    c.store = store
    return c


def _as_anon(client, uid: str) -> None:
    with client.session_transaction() as s:
        s["anon"] = uid


def test_card_does_not_ask_for_login(client):
    """/card анониму не предлагает вход. Без диагностики — объясняет, почему Карты пока нет.

    Раньше здесь был молчаливый редирект на квиз с ?fresh=1: человек жал «Карта стиля» и
    оказывался в начале квиза без объяснений — выглядело как петля.
    """
    r = client.get("/card")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "/login" not in html
    assert "Сначала — диагностика" in html
    assert "identity-scan-quiz.html" in html  # кнопка ведёт в квиз, но человек жмёт её сам


def test_cabinet_does_not_ask_for_login(client):
    r = client.get("/cabinet")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "/login" not in html
    assert "после диагностики" in html


def test_need_diagnosis_screen_does_not_reset_quiz(client):
    """Кнопка ведёт на квиз БЕЗ ?fresh=1 — иначе стирается уже пройденный прогресс."""
    html = client.get("/card").get_data(as_text=True)
    assert "identity-scan-quiz.html?fresh=1" not in html


def test_anon_with_diagnosis_reaches_card(client):
    """Диагностика есть → аноним видит форму сборки Карты, а не стену логина."""
    _as_anon(client, "anon-abc")
    client.store["anon-abc"] = {"diagnosis": DIAG}
    r = client.get("/card")
    assert r.status_code == 200


def test_cabinet_sends_to_card_when_no_card_yet(client):
    """Карты ещё нет → мягко в /card, без петли на квиз."""
    _as_anon(client, "anon-def")
    client.store["anon-def"] = {"diagnosis": DIAG}
    r = client.get("/cabinet")
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/card")


def test_anon_id_is_stable_across_requests(client):
    """Один и тот же аноним между запросами — иначе Карта «теряется» после сборки."""
    client.get("/card")
    with client.session_transaction() as s:
        first = s.get("anon")
    client.get("/card")
    with client.session_transaction() as s:
        assert s.get("anon") == first
    assert first and first.startswith("anon-")


def test_anon_id_never_leaks_into_ui():
    """Технический id наружу не показываем: «для anon-a1b2c3…» в Карте недопустимо."""
    assert m._display_name("anon-a1b2c3") == ""
    assert m._display_name("anna@example.com") == "anna@example.com"


def test_card_shown_to_anon_without_name(client):
    """Готовая Карта открывается анониму и не подписана техническим id."""
    _as_anon(client, "anon-xyz")
    client.store["anon-xyz"] = {"diagnosis": DIAG,
                                "card": {"formula": DIAG["style_formula"], "season": "fw"}}
    r = client.get("/card")
    assert r.status_code == 200
    assert "anon-xyz" not in r.get_data(as_text=True)


def test_free_limit_counts_for_anon(monkeypatch):
    """Бесплатная генерация — одна и для анонима, иначе анонимный доступ = слив токенов."""
    monkeypatch.setattr(m, "_is_admin", lambda: False)
    monkeypatch.setattr(m, "FREE_GEN_LIMIT", 1)
    monkeypatch.setattr(m, "count_generations", lambda e: 0)
    with m.app.test_request_context("/"):
        assert m._gen_allowed("anon-limit") is True
    monkeypatch.setattr(m, "count_generations", lambda e: 1)
    with m.app.test_request_context("/"):
        assert m._gen_allowed("anon-limit") is False


def test_ip_limit_catches_cookie_reset(monkeypatch):
    """Cookie почистили и пришли снова — ловит контур по IP."""
    monkeypatch.setattr(m, "_is_admin", lambda: False)
    monkeypatch.setattr(m, "IP_GEN_LIMIT", 2)
    seen = {"203.0.113.7": 2, "198.51.100.9": 0}
    monkeypatch.setattr(m, "count_generations_ip", lambda ip: seen.get(ip, 0))

    hdr = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}  # за прокси реальный адрес — первый
    with m.app.test_request_context("/", headers=hdr):
        assert m._client_ip() == "203.0.113.7"
        assert m._ip_gen_allowed() is False
    with m.app.test_request_context("/", headers={"X-Forwarded-For": "198.51.100.9"}):
        assert m._ip_gen_allowed() is True
