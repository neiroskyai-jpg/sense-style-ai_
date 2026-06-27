"""Трекинг имиджа: история сессий клиентки и динамика Identity Gap во времени.

Это и есть измеримая трансформация продукта — Gap «было → стало». Лёгкое
SQLite-хранилище: одна запись на прохождение диагностики.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tracking.db"  # data/ в .gitignore


def _conn(db_path: Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(db_path)
    c.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT NOT NULL,
            ts TEXT NOT NULL,
            gap_percentage INTEGER,
            style_formula TEXT,
            colortype TEXT,
            figure_type TEXT
        )"""
    )
    return c


def record_session(client: str, diagnosis: dict, ts: str | None = None,
                   db_path: Path = DB_PATH) -> None:
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO sessions (client, ts, gap_percentage, style_formula, colortype, figure_type)"
            " VALUES (?,?,?,?,?,?)",
            (client, ts, diagnosis.get("gap_percentage"), diagnosis.get("style_formula"),
             diagnosis.get("colortype"), diagnosis.get("figure_type")),
        )


def get_history(client: str, db_path: Path = DB_PATH) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT ts, gap_percentage, style_formula FROM sessions WHERE client=? ORDER BY ts, id",
            (client,),
        ).fetchall()
    return [{"ts": r[0], "gap": r[1], "formula": r[2]} for r in rows]


def progress(client: str, db_path: Path = DB_PATH) -> dict | None:
    """Динамика Identity Gap: первое значение vs последнее. None — если истории нет."""
    h = get_history(client, db_path)
    if not h:
        return None
    first, last = h[0], h[-1]
    delta = (first["gap"] - last["gap"]) if first["gap"] is not None and last["gap"] is not None else None
    return {"sessions": len(h), "first_gap": first["gap"], "last_gap": last["gap"],
            "delta": delta, "history": h}
