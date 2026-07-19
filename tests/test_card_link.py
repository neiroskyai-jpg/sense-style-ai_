"""Постоянная ссылка на Карту: /card/<token>.

Карта жила только в cookie того браузера, где её собрали: клиентка со сменой устройства теряла
результат, а отправить ссылку в мессенджер было нечего. Здесь проверяем главное:
1. ссылка стабильна и переживает пересборку Карты;
2. по ссылке Карта открывается без сессии владельца;
3. открывший ссылку НЕ становится владельцем — иначе это передача аккаунта;
4. на чужом экране нет действий записи (пересборка, отзыв).
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402
from core import profiles as pr  # noqa: E402

OWNER = "anon-owner1"
CARD = {
    "formula": "Классика × Драма",
    "gap": 30,
    "palette": [],
    "looks": [{"scenario": "деловая встреча", "bucket": "Работа", "items": ["Жакет"]}],
    "starter_capsule": [],
    "shopping": [],
    "season_label": "Осень-зима",
}


@pytest.fixture
def client(monkeypatch):
    """Изолированная БД: тесты не должны трогать боевые профили."""
    db = Path(tempfile.mkdtemp()) / "profiles.db"
    m.app.config["TESTING"] = True

    monkeypatch.setattr(m, "card_link_token", lambda e: pr.card_link_token(e, db))
    monkeypatch.setattr(m, "user_by_card_token", lambda t: pr.user_by_card_token(t, db))
    monkeypatch.setattr(m, "get_profile", lambda e: pr.get_profile(e, db))
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)

    pr.save_diagnosis(OWNER, {"style_formula": CARD["formula"], "figure_type": "hourglass"}, db)
    pr.save_card(OWNER, CARD, db)

    with m.app.test_client() as c:
        yield c, db


def test_link_is_stable_across_card_rebuilds(client):
    """Пересобрала Карту — разосланная ссылка обязана остаться рабочей."""
    _, db = client
    token = pr.card_link_token(OWNER, db)

    pr.save_card(OWNER, {**CARD, "gap": 12}, db)  # пересборка

    assert pr.card_link_token(OWNER, db) == token
    assert pr.user_by_card_token(token, db) == OWNER


def test_link_opens_card_without_owner_session(client):
    """Главное: чужой браузер без cookie владельца видит Карту."""
    c, db = client
    token = pr.card_link_token(OWNER, db)

    r = c.get(f"/card/{token}")
    html = r.get_data(as_text=True)

    assert r.status_code == 200
    assert CARD["formula"] in html
    assert r.headers.get("X-Robots-Tag") == "noindex, nofollow", "Карта с фото — не для поиска"


def test_link_does_not_hand_over_the_account(client):
    """Открывший ссылку не становится владельцем: иначе ссылка = передача аккаунта."""
    c, db = client
    token = pr.card_link_token(OWNER, db)

    c.get(f"/card/{token}")

    with c.session_transaction() as s:
        assert s.get("email") != OWNER
        assert s.get("anon") != OWNER


def test_shared_view_has_no_write_actions(client):
    """На чужом экране нечего пересобирать и не за что оставлять отзыв."""
    c, db = client
    token = pr.card_link_token(OWNER, db)

    html = c.get(f"/card/{token}").get_data(as_text=True)

    assert "/card?rebuild=1" not in html
    assert "/card/feedback" not in html
    assert "Ссылка на твою Карту" not in html, "копировать ссылку предлагаем только владелице"


def test_unknown_and_malformed_tokens_do_not_leak(client):
    """Чужой токен ничего не открывает, мусорный путь не притворяется Картой."""
    c, _ = client

    assert c.get("/card/" + "z" * 22).status_code == 404
    assert c.get("/card/nope").status_code == 404


def test_link_route_does_not_shadow_card_status(client):
    """Соседние маршруты /card/... не должны перехватываться ссылкой."""
    c, _ = client

    # /card/status/<job_id> — два сегмента, ссылка его не трогает
    assert c.get("/card/status/deadbeef").status_code != 404
