"""Трекинг имиджа: история сессий клиентки и динамика Identity Gap во времени.

Это и есть измеримая трансформация продукта — Gap «было → стало». Лёгкое
SQLite-хранилище: одна запись на прохождение диагностики.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import data_dir

# на Amvera это persistent-том (/data); определяется автоматически (см. config.data_dir)
DB_PATH = data_dir() / "tracking.db"  # локально data/ в .gitignore


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
    c.execute("CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL)")
    c.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, client TEXT, ts TEXT NOT NULL, meta TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT, ts TEXT NOT NULL, rating INTEGER, text TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS chat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT, ts TEXT NOT NULL, role TEXT, text TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT, ip TEXT, ts TEXT NOT NULL,
            consent_processing INTEGER, consent_transfer INTEGER
        )"""
    )
    return c


def record_consent(client: str, ip: str, processing: bool, transfer: bool,
                   ts: str | None = None, db_path: Path = DB_PATH) -> None:
    """152-ФЗ: журналируем факт получения согласия (кто, IP, время, какие согласия)."""
    ts = ts or datetime.now().isoformat()
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO consents (client, ip, ts, consent_processing, consent_transfer) VALUES (?,?,?,?,?)",
            (client, ip, ts, int(bool(processing)), int(bool(transfer))),
        )


def record_call(db_path: Path = DB_PATH) -> None:
    """Зафиксировать платный вызов (для дневной квоты публичного демо)."""
    with _conn(db_path) as c:
        c.execute("INSERT INTO calls (ts) VALUES (?)", (datetime.now().isoformat(),))


def count_today(db_path: Path = DB_PATH) -> int:
    """Сколько платных вызовов было сегодня (защита от слива ключа на публичном демо)."""
    today = datetime.now().date().isoformat()
    with _conn(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM calls WHERE ts LIKE ?", (today + "%",)).fetchone()[0]


def record_event(name: str, client: str | None = None, meta: str | None = None,
                 ts: str | None = None, db_path: Path = DB_PATH) -> None:
    """Событие воронки (quiz_done, card_form_view, card_built, look_generated, feedback_left…).

    Тихо глотает ошибки: метрика никогда не должна ронять пользовательский поток.
    """
    try:
        ts = ts or datetime.now().isoformat(timespec="seconds")
        with _conn(db_path) as c:
            c.execute("INSERT INTO events (name, client, ts, meta) VALUES (?,?,?,?)",
                      (name, client, ts, meta))
    except Exception:  # noqa: BLE001 — трекинг не критичен для пользователя
        pass


def record_feedback(client: str | None, rating: int | None, text: str | None,
                    ts: str | None = None, db_path: Path = DB_PATH) -> None:
    """Отзыв клиентки о Карте (оценка 1–5 + текст). Питает артефакт «обратная связь» конкурса."""
    ts = ts or datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        c.execute("INSERT INTO feedback (client, ts, rating, text) VALUES (?,?,?,?)",
                  (client, ts, rating, (text or "").strip() or None))


def record_chat(client: str | None, role: str, text: str, ts: str | None = None,
                db_path: Path = DB_PATH) -> None:
    """Сохранить реплику чата «Спросить стилиста» (role=user/assistant). Тихо глотает ошибки."""
    if not (text or "").strip():
        return
    try:
        ts = ts or datetime.now().isoformat(timespec="seconds")
        with _conn(db_path) as c:
            c.execute("INSERT INTO chat (client, ts, role, text) VALUES (?,?,?,?)",
                      (client or "anon", ts, role, text.strip()[:4000]))
    except Exception:  # noqa: BLE001 — чат не должен падать из-за трекинга
        pass


def chat_log(limit: int = 500, db_path: Path = DB_PATH) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT ts, client, role, text FROM chat ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"ts": r[0], "client": r[1], "role": r[2], "text": r[3]} for r in rows]


def count_generations(client: str, names: tuple = ("card_built",), db_path: Path = DB_PATH) -> int:
    """Сколько раз этот email уже запускал дорогую генерацию (по событиям). Для лимита бесплатных прогонов."""
    if not client:
        return 0
    placeholders = ",".join("?" for _ in names)
    with _conn(db_path) as c:
        return c.execute(
            f"SELECT COUNT(*) FROM events WHERE client=? AND name IN ({placeholders})",
            (client, *names),
        ).fetchone()[0]


def leads(db_path: Path = DB_PATH) -> list[dict]:
    """Консолидированный список лидов: email + когда, последняя Формула/Gap/цветотип, число отзывов.
    Источники — sessions (диагностика) + consents (согласие) + feedback. Для выгрузки/связи."""
    with _conn(db_path) as c:
        sess = c.execute(
            "SELECT client, ts, gap_percentage, style_formula, colortype, figure_type "
            "FROM sessions ORDER BY client, ts, id"
        ).fetchall()
        cons = c.execute("SELECT client, MIN(ts), MAX(ts) FROM consents GROUP BY client").fetchall()
        fb = c.execute("SELECT client, COUNT(*) FROM feedback WHERE client IS NOT NULL GROUP BY client").fetchall()
    agg: dict[str, dict] = {}

    def _blank(email):
        return {"email": email, "sessions": 0, "first": None, "last": None,
                "gap": None, "formula": None, "colortype": None, "figure": None, "feedback": 0}

    for client, ts, gap, formula, ct, fig in sess:
        if not client:
            continue
        d = agg.setdefault(client, _blank(client))
        d["sessions"] += 1
        d["first"] = ts if d["first"] is None else min(d["first"], ts)
        d["last"] = ts if d["last"] is None else max(d["last"], ts)
        d.update(gap=gap, formula=formula, colortype=ct, figure=fig)  # последняя (по порядку ts)
    for client, fts, lts in cons:
        if not client:
            continue
        d = agg.setdefault(client, _blank(client))
        d["first"] = fts if d["first"] is None else min(d["first"], fts)
        d["last"] = lts if d["last"] is None else max(d["last"], lts)
    for client, cnt in fb:
        agg.setdefault(client, _blank(client))["feedback"] = cnt
    return sorted(agg.values(), key=lambda d: d["last"] or "", reverse=True)


def funnel(db_path: Path = DB_PATH) -> dict:
    """Числа воронки для приборной панели/конкурса."""
    with _conn(db_path) as c:
        def ev(name):
            return c.execute("SELECT COUNT(*) FROM events WHERE name=?", (name,)).fetchone()[0]
        quiz_done = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        uniq_clients = c.execute("SELECT COUNT(DISTINCT client) FROM sessions").fetchone()[0]
        cards = ev("card_built")
        fb = c.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        avg_rating = c.execute("SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL").fetchone()[0]
    conv = round(100.0 * cards / quiz_done, 1) if quiz_done else 0.0
    return {"quiz_done": quiz_done, "unique_clients": uniq_clients,
            "card_form_view": _ev_count("card_form_view", db_path), "card_built": cards,
            "looks_generated": _ev_count("look_generated", db_path),
            "feedback": fb, "avg_rating": round(avg_rating, 2) if avg_rating else None,
            "quiz_to_card_pct": conv}


def _ev_count(name: str, db_path: Path = DB_PATH) -> int:
    with _conn(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM events WHERE name=?", (name,)).fetchone()[0]


def feedback_list(limit: int = 50, db_path: Path = DB_PATH) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT ts, client, rating, text FROM feedback ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"ts": r[0], "client": r[1], "rating": r[2], "text": r[3]} for r in rows]


def gap_summary(db_path: Path = DB_PATH) -> dict:
    """Средний Identity Gap и динамика «до/после» по клиенткам с ≥2 замерами."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT client, gap_percentage FROM sessions WHERE gap_percentage IS NOT NULL ORDER BY client, ts, id"
        ).fetchall()
    by_client: dict[str, list[int]] = {}
    for client, gap in rows:
        by_client.setdefault(client, []).append(gap)
    all_first = [v[0] for v in by_client.values()]
    deltas = [v[0] - v[-1] for v in by_client.values() if len(v) >= 2]
    return {
        "clients_measured": len(by_client),
        "avg_first_gap": round(sum(all_first) / len(all_first), 1) if all_first else None,
        "clients_with_progress": len(deltas),
        "avg_gap_reduction": round(sum(deltas) / len(deltas), 1) if deltas else None,
    }


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
