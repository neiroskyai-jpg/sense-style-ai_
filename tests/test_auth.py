"""Тесты magic-link токенов и хранилища профиля — без API и без внешней почты."""
import os
import tempfile
from pathlib import Path

from core import auth, profiles


def test_token_roundtrip():
    t = auth.make_token("Anna@Example.com")
    assert auth.read_token(t) == "anna@example.com"  # нормализуется


def test_token_expired():
    t = auth.make_token("a@b.com")
    assert auth.read_token(t, max_age=-1) is None  # просрочен


def test_token_tampered():
    assert auth.read_token("not-a-real-token") is None


def test_profile_roundtrip(tmp_path):
    db = tmp_path / "profiles.db"
    profiles.save_style_profile("u@e.com", {"style_dna": ["Quiet Luxury"]}, db_path=db)
    profiles.save_diagnosis("u@e.com", {"style_formula": "Классика × Драма"}, db_path=db)
    p = profiles.get_profile("u@e.com", db_path=db)
    assert p["style_profile"]["style_dna"] == ["Quiet Luxury"]
    assert p["diagnosis"]["style_formula"] == "Классика × Драма"


def test_profile_empty_for_unknown(tmp_path):
    assert profiles.get_profile("nobody@e.com", db_path=tmp_path / "p.db") == {}


def test_email_not_configured_is_dev_fallback(monkeypatch):
    monkeypatch.delenv("UNISENDER_API_KEY", raising=False)
    monkeypatch.delenv("UNISENDER_FROM_EMAIL", raising=False)
    assert auth.email_configured() is False
    assert auth.send_magic_link("a@b.com", "http://x/auth?token=y") is False  # не упало, dev-режим
