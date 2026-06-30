"""Персональный профиль пользователя: анкета «Примерочной» + Формула — в SQLite.

Хранилище на постоянном томе Amvera (`SENSE_DATA_DIR=/data`), рядом с трекингом.
Ключ — email (нормализованный). Профиль и Формула лежат как JSON. Без паролей —
идентификация через сессию после magic-link (см. core/auth.py).
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import data_dir

DB_PATH = data_dir() / "profiles.db"


def _conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS profiles ("
        " email TEXT PRIMARY KEY,"
        " style_profile TEXT,"   # JSON: анкета Примерочной (линии/ДНК/анти-гардероб)
        " diagnosis TEXT,"       # JSON: последняя Формула стиля
        " card TEXT,"            # JSON: собранная «Карта стиля» (кэш)
        " updated_at TEXT)"
    )
    # миграция: добавить колонку card в существующую БД, если её нет
    cols = {r[1] for r in con.execute("PRAGMA table_info(profiles)").fetchall()}
    if "card" not in cols:
        con.execute("ALTER TABLE profiles ADD COLUMN card TEXT")
    return con


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def get_profile(email: str, db_path: Path = DB_PATH) -> dict:
    """Профиль пользователя: {style_profile, diagnosis} (пустые dict, если нет)."""
    if not _norm(email):
        return {}
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT style_profile, diagnosis, card FROM profiles WHERE email=?", (_norm(email),)
        ).fetchone()
    if not row:
        return {}
    return {
        "style_profile": json.loads(row[0]) if row[0] else {},
        "diagnosis": json.loads(row[1]) if row[1] else {},
        "card": json.loads(row[2]) if row[2] else {},
    }


def _upsert(email: str, field: str, value: dict, db_path: Path) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(value, ensure_ascii=False)
    with _conn(db_path) as con:
        con.execute(
            f"INSERT INTO profiles (email, {field}, updated_at) VALUES (?, ?, ?) "
            f"ON CONFLICT(email) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at",
            (_norm(email), payload, now),
        )
        con.commit()


def save_style_profile(email: str, style_profile: dict, db_path: Path = DB_PATH) -> None:
    if _norm(email):
        _upsert(email, "style_profile", style_profile, db_path)


def save_diagnosis(email: str, diagnosis: dict, db_path: Path = DB_PATH) -> None:
    if _norm(email):
        _upsert(email, "diagnosis", diagnosis, db_path)


def save_card(email: str, card: dict, db_path: Path = DB_PATH) -> None:
    if _norm(email):
        _upsert(email, "card", card, db_path)
