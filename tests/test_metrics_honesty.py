"""Метрики продукта считают живых клиенток, а не нашу собственную работу.

Реальная проблема (найдена 16.07.2026 при подготовке слайдов для жюри): воронка считала всё
подряд. Из 12 «прохождений квиза» три были smoke-*@test.local, плюс самотест автора; средний
стартовый Gap 69.8% был раздут именно ими (у смоук-прогонов 76–78%), честный — 62%.
На защите такую цифру разбирают первым же уточняющим вопросом.
"""
import os
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from core.tracking import funnel, gap_summary, record_event, record_session  # noqa: E402


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "tracking.db"


def _session(db, client, gap, ts):
    record_session(client, {"gap_percentage": gap, "style_formula": "X"}, ts=ts, db_path=db)


def test_smoke_i_samotesty_ne_schitayutsya_klientkami(db):
    _session(db, "client@mail.ru", 50, "2026-07-16T16:32:46")
    _session(db, "smoke-face@test.local", 78, "2026-07-10T21:21:47")
    _session(db, "smoke-prod@test.local", 76, "2026-07-10T19:02:52")
    _session(db, "neiroskyai@gmail.com", 78, "2026-06-30T22:40:50")   # автор проекта
    _session(db, "anonymous", 60, "2026-07-16T17:03:40")

    f = funnel(db)
    assert f["quiz_done"] == 1, "в воронку попали тесты/автор/аноним"
    assert f["unique_clients"] == 1
    assert f["excluded_technical"] == 4, "число отфильтрованных должно быть проверяемым"


def test_sredniy_gap_ne_razduvaetsya_testami(db):
    """Честное среднее — только по клиенткам: (50+60+78+60)/4 = 62.0, а не 69.8."""
    for client, gap in [("a@mail.ru", 50), ("b@mail.ru", 60), ("c@mail.ru", 78), ("d@mail.ru", 60)]:
        _session(db, client, gap, "2026-07-15T12:00:00")
    for client, gap in [("smoke-1@test.local", 78), ("smoke-2@test.local", 76)]:
        _session(db, client, gap, "2026-07-10T12:00:00")

    g = gap_summary(db)
    assert g["clients_measured"] == 4
    assert g["avg_first_gap"] == 62.0


def test_povtor_v_tot_zhe_den_pomechen_kak_shum(db):
    """Два замера за полчаса — не «до/после»: между ними ничего не произошло."""
    _session(db, "same@mail.ru", 50, "2026-07-16T16:32:46")
    _session(db, "same@mail.ru", 50, "2026-07-16T17:05:52")   # шум одного дня
    _session(db, "long@mail.ru", 78, "2026-06-30T12:26:38")
    _session(db, "long@mail.ru", 70, "2026-07-15T13:11:04")   # настоящий лонгитюд

    g = gap_summary(db)
    assert g["clients_with_progress"] == 2
    assert g["same_day_repeats"] == 1, "повтор в тот же день обязан быть отделим от лонгитюда"


def test_konversiya_schitaetsya_po_realnym(db):
    _session(db, "client@mail.ru", 50, "2026-07-16T10:00:00")
    _session(db, "smoke@test.local", 78, "2026-07-16T10:00:00")
    record_event("card_built", "client@mail.ru", db_path=db)
    record_event("card_built", "smoke@test.local", db_path=db)   # наша же проверка прода

    f = funnel(db)
    assert f["card_built"] == 1, "Карты из смоук-тестов не должны считаться выдачей клиенткам"
    assert f["quiz_to_card_pct"] == 100.0
