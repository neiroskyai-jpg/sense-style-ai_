"""Правило 60/30/10: сколько каждого поля формулы в образе.

Формула комбинаторная («Классика × Драма × Натуральный»), и требования «покрой все поля» мало:
без заданных долей образ выходит размазанным — ни одно поле не ведёт, а формула читается как
склейка. Доли фиксированные: доминанта держит силуэт, вторичное даёт фактуру, акцент — ровно
один элемент.

Источник правила — каркас базы стилей от фаундера.
"""
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402

LOOK_GENERATOR = io.open("architecture/prompts/look-generator.md", encoding="utf-8").read()


def test_six_looks_carry_the_proportion():
    assert "ПРОПОРЦИЯ 60 / 30 / 10" in LOOK_GENERATOR
    assert "Задаёт СИЛУЭТ и базовую палитру" in LOOK_GENERATOR
    assert "РОВНО ОДИН выразительный элемент" in LOOK_GENERATOR


def test_two_directions_carry_the_proportion():
    """Витрина после квиза — тот же принцип: без пропорции два образа выглядят случайными."""
    assert "ПРОПОРЦИЯ 60/30/10" in p._DIRECTIONS_SYSTEM
    assert "РОВНО ОДИН выразительный элемент" in p._DIRECTIONS_SYSTEM


def test_dominant_is_taken_from_diagnosis_not_guessed():
    """Доминанта берётся из распределения полей, а не выбирается моделью на глаз."""
    assert "semantic_field_distribution" in LOOK_GENERATOR
    assert "semantic_field_distribution" in p._DIRECTIONS_SYSTEM


def test_two_field_formula_has_its_own_split():
    """Формула из двух полей не должна делиться на три: третьего поля просто нет."""
    assert "70 доминанта / 30 вторичное" in LOOK_GENERATOR
    assert "70 доминанта / 30 вторичное" in p._DIRECTIONS_SYSTEM
