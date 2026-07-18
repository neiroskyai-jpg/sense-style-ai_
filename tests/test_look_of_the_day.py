"""«Образ дня» в кабинете — фото, а не текст, и без новой генерации.

Фаундер ожидала кабинет как на макетах: с фото образов на клиентке. Рендерить их заново дорого
(~30с и деньги за кадр), поэтому берём уже готовые образы Карты и раскладываем по дням недели.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

CARD = {"looks": [
    {"scenario": "деловая встреча", "img": "A", "desc": "Жакет и брюки"},
    {"scenario": "свидание", "img": "B", "desc": "Платье"},
    {"scenario": "выходные", "img": "C", "desc": "Джинсы и свитер"},
]}


def test_returns_look_with_photo():
    look = m._look_of_the_day(CARD)
    assert look and look["img"] in {"A", "B", "C"}
    assert look["scenario"]


def test_no_photo_no_block():
    """Образы без фото не годятся: блок просто не показываем, текстовую заглушку не рисуем."""
    assert m._look_of_the_day({"looks": [{"scenario": "работа"}]}) is None
    assert m._look_of_the_day({}) is None


def test_frost_excludes_open_scenario():
    """В мороз «свидание» — плохой совет дня.

    Раньше ротация шла индексом дня по общему списку и перебивала приоритет: исключённый
    сценарий всё равно мог выпасть.
    """
    assert m._look_of_the_day(CARD, {"feels_like": -9})["scenario"] != "свидание"


def test_weekday_prefers_business(monkeypatch):
    """В будни — деловые сценарии, в выходные — свободные."""
    import datetime as real_dt

    class Monday(real_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 20)  # понедельник

    monkeypatch.setattr(real_dt, "date", Monday)
    assert m._look_of_the_day(CARD)["scenario"] == "деловая встреча"

    class Sunday(real_dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 19)  # воскресенье

    monkeypatch.setattr(real_dt, "date", Sunday)
    assert m._look_of_the_day(CARD)["scenario"] in {"выходные", "свидание"}
