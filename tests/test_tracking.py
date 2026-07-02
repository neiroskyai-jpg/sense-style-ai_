"""Тесты трекинга имиджа (SQLite, без обращения к API)."""
from core.tracking import (count_today, gap_progress, gap_timeline, get_history,
                           progress, record_call, record_session)


def test_call_quota_counter(tmp_path):
    db = tmp_path / "t.db"
    assert count_today(db_path=db) == 0
    record_call(db_path=db)
    record_call(db_path=db)
    assert count_today(db_path=db) == 2


def test_record_consent(tmp_path):
    from core.tracking import _conn, record_consent
    db = tmp_path / "t.db"
    record_consent("anna@example.com", "1.2.3.4", True, True,
                   ts="2026-06-27T10:00:00", db_path=db)
    with _conn(db) as c:
        row = c.execute("SELECT client, ip, consent_processing, consent_transfer FROM consents").fetchone()
    assert row == ("anna@example.com", "1.2.3.4", 1, 1)


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


# --- Фаза 3: целостность трекера (точка = реальный замер, дельта только при ≥2) ---

def test_timeline_collapses_echo_of_same_measurement(tmp_path):
    # квиз + сборка Карты + лид пишут сессии с ОДНИМ gap — это один замер, не три точки
    db = tmp_path / "t.db"
    for _ in range(3):
        record_session("anna", {"gap_percentage": 41, "style_formula": "A"}, db_path=db)
    pts = gap_timeline("anna", db_path=db)
    assert len(pts) == 1 and pts[0]["gap"] == 41


def test_single_measurement_has_no_delta(tmp_path):
    # один реальный замер → точка отсчёта без дельты (нельзя обещать динамику)
    db = tmp_path / "t.db"
    record_session("anna", {"gap_percentage": 41}, ts="2026-07-01T10:00:00", db_path=db)
    p = gap_progress("anna", db_path=db)
    assert p["measurements"] == 1
    assert p["current_gap"] == 41
    assert p["delta"] is None


def test_two_real_measurements_give_delta(tmp_path):
    db = tmp_path / "t.db"
    record_session("anna", {"gap_percentage": 64}, ts="2026-01-18T10:00:00", db_path=db)
    record_session("anna", {"gap_percentage": 64}, ts="2026-01-18T10:05:00", db_path=db)  # эхо
    record_session("anna", {"gap_percentage": 18}, ts="2026-05-04T10:00:00", db_path=db)
    p = gap_progress("anna", db_path=db)
    assert p["measurements"] == 2  # эхо схлопнулось
    assert p["first_gap"] == 64 and p["current_gap"] == 18
    assert p["delta"] == 46


def test_gap_progress_none_without_history(tmp_path):
    db = tmp_path / "t.db"
    assert gap_progress("nobody", db_path=db) is None
