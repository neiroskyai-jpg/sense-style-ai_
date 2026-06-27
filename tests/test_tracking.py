"""Тесты трекинга имиджа (SQLite, без обращения к API)."""
from core.tracking import (count_today, get_history, progress, record_call,
                           record_session)


def test_call_quota_counter(tmp_path):
    db = tmp_path / "t.db"
    assert count_today(db_path=db) == 0
    record_call(db_path=db)
    record_call(db_path=db)
    assert count_today(db_path=db) == 2


def test_progress_tracks_gap_over_time(tmp_path):
    db = tmp_path / "t.db"
    record_session("anna@example.com", {"gap_percentage": 75, "style_formula": "A"},
                   ts="2026-06-01T10:00:00", db_path=db)
    record_session("anna@example.com", {"gap_percentage": 40, "style_formula": "A"},
                   ts="2026-07-01T10:00:00", db_path=db)

    p = progress("anna@example.com", db_path=db)
    assert p["sessions"] == 2
    assert p["first_gap"] == 75
    assert p["last_gap"] == 40
    assert p["delta"] == 35  # разрыв сократился на 35 п.п.


def test_history_isolated_per_client(tmp_path):
    db = tmp_path / "t.db"
    record_session("a", {"gap_percentage": 50}, db_path=db)
    record_session("b", {"gap_percentage": 60}, db_path=db)
    assert len(get_history("a", db_path=db)) == 1
    assert progress("nobody", db_path=db) is None
