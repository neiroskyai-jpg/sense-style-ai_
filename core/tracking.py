"""Трекинг имиджа: история сессий клиентки и динамика Identity Gap во времени.

Это и есть измеримая трансформация продукта — Gap «было → стало». Лёгкое
SQLite-хранилище: одна запись на прохождение диагностики.
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .config import data_dir

# на Amvera это persistent-том (/data); определяется автоматически (см. config.data_dir)
DB_PATH = data_dir() / "tracking.db"  # локально data/ в .gitignore

# ── Кто НЕ клиентка ────────────────────────────────────────────────────────────────────────────
# Метрики продукта должны считать живых пользовательниц. Раньше в воронку попадали смоук-тесты и
# самотесты автора: из 12 «прохождений квиза» три были smoke-*@test.local, а средний стартовый Gap
# 69.8% был раздут ими (у смоук-прогонов 76–78%). На защите такую цифру разбирают первым же
# вопросом — честные 8 прохождений дороже раздутых 12.
_TEST_PATTERNS = ("smoke-%", "%@test.local", "%@example.com", "test@%", "lead@test.ru", "leadtest_%")
_NON_CLIENT = ("anonymous", "")


def _real_client_sql(col: str = "client") -> str:
    """SQL-условие «это живая клиентка»: не тест, не аноним, не автор проекта.

    Автора (SENSE_ADMIN_EMAILS) исключаем из воронки: её самотесты — валидация алгоритма, а не
    пользовательский спрос. Данные при этом не удаляются, их видно отдельной строкой в /metrics.
    """
    import os
    admins = [e.strip().lower() for e in
              os.getenv("SENSE_ADMIN_EMAILS", "neiroskyai@gmail.com").split(",") if e.strip()]
    parts = [f"LOWER({col}) NOT LIKE '{p}'" for p in _TEST_PATTERNS]
    parts += [f"LOWER({col}) != '{n}'" for n in _NON_CLIENT]
    parts += [f"LOWER({col}) != '{a}'" for a in admins]
    parts.append(f"{col} IS NOT NULL")
    return " AND ".join(parts)


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
    # approved: 0 — не модерирован, 1 — одобрен к публичному показу на лендинге. Миграция для старых БД.
    try:
        c.execute("ALTER TABLE feedback ADD COLUMN approved INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже есть
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
    # Журнал дорогих генераций с IP — второй контур лимита. Первый контур (по client) держится на
    # cookie, а её можно почистить; без IP-контура анонимный доступ = безлимитный слив токенов.
    c.execute(
        """CREATE TABLE IF NOT EXISTS gen_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client TEXT, ip TEXT, ts TEXT NOT NULL
        )"""
    )
    return c


def record_generation(client: str, ip: str, ts: str | None = None, db_path: Path = DB_PATH) -> None:
    """Зафиксировать дорогую генерацию (с образами) для лимита по устройству и по IP."""
    ts = ts or datetime.now().isoformat()
    try:
        with _conn(db_path) as c:
            c.execute("INSERT INTO gen_log (client, ip, ts) VALUES (?,?,?)", (client, ip or "", ts))
    except sqlite3.Error:
        pass  # учёт лимита не должен ронять пользовательский поток


def count_generations_ip(ip: str, hours: int = 24, db_path: Path = DB_PATH) -> int:
    """Сколько дорогих генераций ушло с этого IP за последние `hours` часов."""
    if not ip:
        return 0
    since = (datetime.now() - timedelta(hours=hours)).isoformat()
    with _conn(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM gen_log WHERE ip=? AND ts >= ?", (ip, since)
        ).fetchone()[0]


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
    """Сколько раз этот email уже запускал ДОРОГУЮ генерацию (с образами). Для лимита бесплатных.

    Текстовая Карта (`meta='text'`) сюда НЕ считается: она не рендерит образы и почти ничего не
    стоит. Раньше считалась — и клиентка, нажавшая «собрать пока без образов», сжигала на тексте
    свою единственную бесплатную генерацию: вернуться за образами было уже нельзя. Ровно об этом
    два отзыва («нет генерации образов, только текст», «не хватило визуальных примеров»).
    """
    if not client:
        return 0
    placeholders = ",".join("?" for _ in names)
    with _conn(db_path) as c:
        return c.execute(
            f"SELECT COUNT(*) FROM events WHERE client=? AND name IN ({placeholders})"
            " AND COALESCE(meta, '') != 'text'",
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
        mkt = {r[0] for r in c.execute(
            "SELECT DISTINCT client FROM events WHERE name='marketing_optin' AND client IS NOT NULL").fetchall()}
    agg: dict[str, dict] = {}

    def _blank(email):
        return {"email": email, "sessions": 0, "first": None, "last": None, "gap": None,
                "formula": None, "colortype": None, "figure": None, "feedback": 0,
                "marketing": email in mkt}

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
    """Числа воронки для приборной панели/конкурса — только по живым клиенткам.

    Смоук-тесты и самотесты автора исключены (см. _real_client_sql): иначе воронка меряет нашу же
    работу, а не спрос. Сколько отфильтровано — отдаём отдельно (`excluded_technical`), чтобы
    цифра была проверяемой, а не «мы что-то там убрали».
    """
    real = _real_client_sql()
    with _conn(db_path) as c:
        def ev(name):
            return c.execute(
                f"SELECT COUNT(*) FROM events WHERE name=? AND {real}", (name,)).fetchone()[0]
        quiz_done = c.execute(f"SELECT COUNT(*) FROM sessions WHERE {real}").fetchone()[0]
        uniq_clients = c.execute(f"SELECT COUNT(DISTINCT client) FROM sessions WHERE {real}").fetchone()[0]
        total_sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        cards = ev("card_built")
        fb = c.execute(f"SELECT COUNT(*) FROM feedback WHERE {real}").fetchone()[0]
        avg_rating = c.execute(
            f"SELECT AVG(rating) FROM feedback WHERE rating IS NOT NULL AND {real}").fetchone()[0]
        forms = c.execute(f"SELECT COUNT(*) FROM events WHERE name='card_form_view' AND {real}").fetchone()[0]
        looks = c.execute(f"SELECT COUNT(*) FROM events WHERE name='look_generated' AND {real}").fetchone()[0]
    conv = round(100.0 * cards / quiz_done, 1) if quiz_done else 0.0
    return {"quiz_done": quiz_done, "unique_clients": uniq_clients,
            "card_form_view": forms, "card_built": cards,
            "looks_generated": looks,
            "feedback": fb, "avg_rating": round(avg_rating, 2) if avg_rating else None,
            "quiz_to_card_pct": conv,
            "excluded_technical": total_sessions - quiz_done}


def _ev_count(name: str, db_path: Path = DB_PATH) -> int:
    with _conn(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM events WHERE name=?", (name,)).fetchone()[0]


def feedback_list(limit: int = 50, db_path: Path = DB_PATH) -> list[dict]:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT id, ts, client, rating, text, approved FROM feedback ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "ts": r[1], "client": r[2], "rating": r[3],
             "text": r[4], "approved": bool(r[5])} for r in rows]


def set_feedback_approved(fid: int, approved: bool, db_path: Path = DB_PATH) -> None:
    """Модерация: одобрить/снять отзыв для публичного показа на лендинге."""
    with _conn(db_path) as c:
        c.execute("UPDATE feedback SET approved=? WHERE id=?", (int(bool(approved)), fid))


def approved_feedback(limit: int = 12, db_path: Path = DB_PATH) -> list[dict]:
    """Одобренные отзывы С ТЕКСТОМ — для публичного блока на лендинге. Без email (приватность)."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT ts, rating, text FROM feedback WHERE approved=1 AND text IS NOT NULL AND text!='' "
            "ORDER BY ts DESC, id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [{"ts": r[0], "rating": r[1], "text": r[2]} for r in rows]


def gap_summary(db_path: Path = DB_PATH) -> dict:
    """Средний Identity Gap и динамика «до/после» — только по живым клиенткам.

    Смоук-прогоны раздували среднее (у них Gap 76–78%), самотесты автора — тоже. Плюс отдаём
    `same_day_repeats`: повторный замер в тот же день — не «до/после», а шум (между замерами
    ничего не произошло), и выдавать его за трансформацию нельзя.
    """
    real = _real_client_sql()
    with _conn(db_path) as c:
        rows = c.execute(
            f"SELECT client, gap_percentage, ts FROM sessions"
            f" WHERE gap_percentage IS NOT NULL AND {real} ORDER BY client, ts, id"
        ).fetchall()
    by_client: dict[str, list[tuple]] = {}
    for client, gap, ts in rows:
        by_client.setdefault(client, []).append((gap, ts))
    all_first = [v[0][0] for v in by_client.values()]
    repeats = [v for v in by_client.values() if len(v) >= 2]
    deltas = [v[0][0] - v[-1][0] for v in repeats]
    same_day = sum(1 for v in repeats if str(v[0][1])[:10] == str(v[-1][1])[:10])
    return {
        "clients_measured": len(by_client),
        "avg_first_gap": round(sum(all_first) / len(all_first), 1) if all_first else None,
        "clients_with_progress": len(deltas),
        "avg_gap_reduction": round(sum(deltas) / len(deltas), 1) if deltas else None,
        "same_day_repeats": same_day,   # столько «повторов» — шум одного дня, не лонгитюд
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


def gap_timeline(client: str, db_path: Path = DB_PATH) -> list[dict]:
    """Точки трекера = РАЗНЫЕ замеры разрыва. Подряд идущие сессии с одинаковым gap — это
    один и тот же замер (эхо из сборки Карты/лида на том же диагнозе), схлопываем в одну точку.
    Разрыв двигается только реальным пере-замером (новые фото), иначе трекер показывает фикцию."""
    pts: list[dict] = []
    for h in get_history(client, db_path):
        if h["gap"] is None:
            continue
        if pts and pts[-1]["gap"] == h["gap"]:
            continue  # то же значение подряд — не новый замер
        pts.append({"ts": h["ts"], "gap": h["gap"], "formula": h["formula"]})
    return pts


def gap_progress(client: str, db_path: Path = DB_PATH) -> dict | None:
    """Динамика разрыва для кабинета. Дельту («−N п.п.») отдаём ТОЛЬКО при ≥2 реальных замерах —
    при одном замере это лишь точка отсчёта, обещать динамику нельзя. None — если замеров нет."""
    pts = gap_timeline(client, db_path)
    if not pts:
        return None
    first, last = pts[0], pts[-1]
    measured = len(pts)
    delta = (first["gap"] - last["gap"]) if measured >= 2 else None
    return {"points": pts, "measurements": measured,
            "first_gap": first["gap"], "current_gap": last["gap"], "delta": delta}
