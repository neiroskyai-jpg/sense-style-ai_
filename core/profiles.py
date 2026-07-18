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
    # история версий Карты (капсула была → обновление; снимок на каждую сборку, с сезоном)
    con.execute(
        "CREATE TABLE IF NOT EXISTS card_history ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " email TEXT, ts TEXT, season TEXT, card TEXT)"
    )
    # Личный гардероб: вещи, которые клиентка решила взять после проверки «брать / не брать».
    # Без него проверка была разговором без последствий: вердикт есть, а вещь никуда не девается
    # и в капсуле не участвует.
    con.execute(
        "CREATE TABLE IF NOT EXISTS wardrobe ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " email TEXT, ts TEXT, name TEXT, slot TEXT, verdict TEXT, reason TEXT, image TEXT)"
    )
    return con


def add_wardrobe_item(email: str, item: dict, db_path: Path = DB_PATH) -> None:
    """Добавить вещь в личный гардероб клиентки (после «брать»)."""
    if not _norm(email) or not (item or {}).get("name"):
        return
    with _conn(db_path) as con:
        con.execute(
            "INSERT INTO wardrobe (email, ts, name, slot, verdict, reason, image) VALUES (?,?,?,?,?,?,?)",
            (_norm(email), datetime.now(timezone.utc).isoformat(), item.get("name"),
             item.get("slot") or "", item.get("verdict") or "", item.get("reason") or "",
             item.get("image") or ""),
        )
        con.commit()


def wardrobe_items(email: str, db_path: Path = DB_PATH) -> list[dict]:
    """Личный гардероб: вещи клиентки, новые сверху."""
    if not _norm(email):
        return []
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT id, ts, name, slot, verdict, reason, image FROM wardrobe WHERE email=? "
            "ORDER BY id DESC", (_norm(email),)
        ).fetchall()
    return [{"id": r[0], "ts": r[1], "name": r[2], "slot": r[3],
             "verdict": r[4], "reason": r[5], "image": r[6]} for r in rows]


def delete_wardrobe_item(email: str, item_id: int, db_path: Path = DB_PATH) -> None:
    """Убрать вещь из гардероба (передумала)."""
    if not _norm(email):
        return
    with _conn(db_path) as con:
        con.execute("DELETE FROM wardrobe WHERE email=? AND id=?", (_norm(email), item_id))
        con.commit()


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
        # снимок в историю — чтобы кабинет показывал «капсула была → обновление» (сезон из карты)
        append_card_version(email, card, season=(card or {}).get("season"), db_path=db_path)


def append_card_version(email: str, card: dict, season: str | None = None,
                        ts: str | None = None, db_path: Path = DB_PATH) -> None:
    """Снимок Карты в историю версий (капсула+шопинг+палитра) с меткой времени и сезоном."""
    if not _norm(email):
        return
    ts = ts or datetime.now(timezone.utc).isoformat()
    with _conn(db_path) as con:
        con.execute(
            "INSERT INTO card_history (email, ts, season, card) VALUES (?,?,?,?)",
            (_norm(email), ts, season, json.dumps(card, ensure_ascii=False)),
        )
        con.commit()


def card_versions(email: str, db_path: Path = DB_PATH) -> list[dict]:
    """История версий Карты по времени (старые → новые). Каждая: {ts, season, card}."""
    if not _norm(email):
        return []
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT ts, season, card FROM card_history WHERE email=? ORDER BY ts, id",
            (_norm(email),),
        ).fetchall()
    return [{"ts": r[0], "season": r[1], "card": json.loads(r[2]) if r[2] else {}}
            for r in rows]


def current_card_by_season(email: str, db_path: Path = DB_PATH) -> dict:
    """Последняя версия Карты для каждого сезона: {season: card}. Сезон None → ключ ''."""
    out: dict[str, dict] = {}
    for v in card_versions(email, db_path):  # по возрастанию времени → последняя перезапишет
        out[v.get("season") or ""] = v.get("card") or {}
    return out
