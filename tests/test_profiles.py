"""Тесты профиля и версионирования Карты (SQLite, без API)."""
from core.profiles import (append_card_version, card_versions,
                           current_card_by_season, get_profile, save_card)


def test_save_card_also_appends_version(tmp_path):
    db = tmp_path / "p.db"
    save_card("anna@example.com", {"formula": "A", "season": "ss"}, db_path=db)
    assert get_profile("anna@example.com", db_path=db)["card"]["formula"] == "A"
    vers = card_versions("anna@example.com", db_path=db)
    assert len(vers) == 1 and vers[0]["season"] == "ss"


def test_card_versions_ordered_over_time(tmp_path):
    db = tmp_path / "p.db"
    append_card_version("anna", {"formula": "v1"}, ts="2026-01-01T10:00:00", db_path=db)
    append_card_version("anna", {"formula": "v2"}, ts="2026-03-01T10:00:00", db_path=db)
    vers = card_versions("anna", db_path=db)
    assert [v["card"]["formula"] for v in vers] == ["v1", "v2"]  # старые → новые


def test_current_card_by_season_keeps_latest_per_season(tmp_path):
    db = tmp_path / "p.db"
    append_card_version("anna", {"n": 1}, season="ss", ts="2026-01-01T10:00:00", db_path=db)
    append_card_version("anna", {"n": 2}, season="fw", ts="2026-02-01T10:00:00", db_path=db)
    append_card_version("anna", {"n": 3}, season="ss", ts="2026-05-01T10:00:00", db_path=db)  # обновили ss
    by = current_card_by_season("anna", db_path=db)
    assert by["ss"]["n"] == 3  # последняя весна-лето
    assert by["fw"]["n"] == 2


def test_versions_isolated_per_email(tmp_path):
    db = tmp_path / "p.db"
    append_card_version("a", {"x": 1}, db_path=db)
    append_card_version("b", {"x": 2}, db_path=db)
    assert len(card_versions("a", db_path=db)) == 1
    assert card_versions("nobody", db_path=db) == []
