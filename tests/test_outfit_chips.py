"""Объяснения под образом дня: клиентка видит связь «условие → следствие».

Из ТЗ фаундера (пункт 4): образ дня должен нести explainable-слой — «30° → лёгкие ткани»,
«роль: встреча → собранный силуэт». Раньше кабинет показывал готовый образ и погоду рядом,
но не объяснял, как одно повлияло на другое.

Числа и правила считает код, а не модель: при одних и тех же входных данных объяснение
обязано быть тем же.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

HOT = {"temp": 30.2, "wind": 2, "is_rain": False, "is_snow": False}
FROST = {"temp": -6.0, "wind": 9, "is_rain": False, "is_snow": True}


def _labels(chips):
    return " | ".join(c["label"] for c in chips)


def test_heat_explains_light_fabrics():
    chips = m.outfit_chips(HOT, None, None)

    assert "30° — жарко" in _labels(chips)
    assert "лёгкие ткани" in chips[0]["why"]


def test_frost_requires_outerwear():
    chips = m.outfit_chips(FROST, None, None)
    labels = _labels(chips)

    assert "-6° — мороз" in labels
    assert "снег" in labels and "ветрено" in labels
    assert any("пальто" in c["why"] or "пуховик" in c["why"] for c in chips)


def test_role_explains_silhouette():
    """Роль объясняет требование к силуэту, а не просто называется."""
    chips = m.outfit_chips(None, "Работа", None)

    assert chips[0]["label"] == "роль: работа"
    assert "силуэт" in chips[0]["why"]


def test_mood_is_explained_through_the_method():
    chips = m.outfit_chips(None, None, "властная")

    assert "структура" in chips[0]["why"], "объясняем через приём, а не через эмоцию"


def test_chips_are_deterministic():
    """Одни входные данные — одно объяснение. Всегда."""
    first = m.outfit_chips(FROST, "Выход", "элегантная")

    for _ in range(5):
        assert m.outfit_chips(FROST, "Выход", "элегантная") == first


def test_unknown_inputs_stay_silent():
    """Нет данных — молчим, а не выдумываем объяснение."""
    assert m.outfit_chips(None, None, None) == []
    assert m.outfit_chips({"temp": None}, "Неизвестная роль", "неизвестное") == []


def test_cabinet_renders_the_block():
    assert "Почему образ такой" in m.CABINET_PAGE
