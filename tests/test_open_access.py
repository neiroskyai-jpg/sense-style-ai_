"""Режим открытого доступа (тест-период): ввод почты пускает в кабинет без клика по письму.

Причина: magic-link на телефоне рвёт сессию (клиентка уходит в почту и не возвращается), а пока
SMTP не починен — письма не доходят вовсе. На время прогонов впускаем по вводу почты. Это
осознанный компромисс ТОЛЬКО для теста, поэтому по умолчанию режим ВЫКЛ.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    m.app.config["TESTING"] = True
    store: dict = {}
    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "record_session", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "make_token", lambda e: "tok")
    monkeypatch.setattr(m, "send_magic_link", lambda e, link: True)
    return m.app.test_client()


def test_vyklyuchen_po_umolchaniyu(monkeypatch):
    monkeypatch.delenv("SENSE_OPEN_ACCESS", raising=False)
    assert m._open_access() is False


def test_login_bez_otkrytogo_dostupa_ne_puskaet(client, monkeypatch):
    """Обычный режим: /login не логинит в сессию — ждёт клик по письму."""
    monkeypatch.delenv("SENSE_OPEN_ACCESS", raising=False)
    r = client.post("/login", data={"email": "a@mail.ru"})
    with client.session_transaction() as s:
        assert "email" not in s
    assert r.status_code == 200      # показана страница «проверь почту», без редиректа в кабинет


def test_login_s_otkrytym_dostupom_puskaet_srazu(client, monkeypatch):
    """Тест-режим: ввод почты → сразу в сессию и редирект в кабинет."""
    monkeypatch.setenv("SENSE_OPEN_ACCESS", "1")
    r = client.post("/login", data={"email": "a@mail.ru", "next": "/card"})
    with client.session_transaction() as s:
        assert s.get("email") == "a@mail.ru"
    assert r.status_code in (301, 302)
    assert "/card" in r.headers["Location"]


def test_lead_s_otkrytym_dostupom_loginit(client, monkeypatch):
    """Почта на экране квиза при открытом доступе тоже пускает — чтобы «Получить Карту» вело в Карту."""
    monkeypatch.setenv("SENSE_OPEN_ACCESS", "1")
    r = client.post("/lead", json={"email": "b@mail.ru", "job_id": None})
    assert r.get_json().get("ok") is True
    with client.session_transaction() as s:
        assert s.get("email") == "b@mail.ru"


def test_lead_bez_dostupa_ne_loginit(client, monkeypatch):
    monkeypatch.delenv("SENSE_OPEN_ACCESS", raising=False)
    client.post("/lead", json={"email": "c@mail.ru", "job_id": None})
    with client.session_transaction() as s:
        assert "email" not in s
