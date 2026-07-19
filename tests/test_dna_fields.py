"""ДНК стиля в долях: из чего собран стиль клиентки.

Формула называет направление, но не показывает пропорции. Доли четырёх полей метода — это и
есть результат ДНК-теста, он уже считается диагностикой и просто не выводился на экран.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402


def test_fields_are_sorted_by_share():
    diag = {"semantic_field_distribution": {"classic": 45, "drama": 30, "romance": 15, "natural": 10}}

    fields = m._dna_fields(diag)

    assert [f["label"] for f in fields] == ["Классика", "Драма", "Романтика", "Натуральный"]
    assert [f["pct"] for f in fields] == [45, 30, 15, 10]


def test_shares_are_normalised_to_hundred():
    """Модель отдаёт то 98, то 103 — полоса обязана складываться в 100%."""
    diag = {"semantic_field_distribution": {"classic": 50, "drama": 30, "romance": 20, "natural": 3}}

    fields = m._dna_fields(diag)

    assert sum(f["pct"] for f in fields) == 100


def test_zero_fields_are_dropped():
    """Поле с нулём — не часть ДНК, в легенде ему делать нечего."""
    diag = {"semantic_field_distribution": {"classic": 70, "drama": 30, "romance": 0, "natural": 0}}

    fields = m._dna_fields(diag)

    assert [f["label"] for f in fields] == ["Классика", "Драма"]


def test_old_diagnosis_without_distribution_gives_nothing():
    """Карты, собранные до этого, не должны падать — просто нет блока."""
    assert m._dna_fields({}) == []
    assert m._dna_fields({"semantic_field_distribution": {}}) == []


def test_every_field_has_a_colour():
    diag = {"semantic_field_distribution": {"classic": 25, "drama": 25, "romance": 25, "natural": 25}}

    assert all(f["hex"].startswith("#") for f in m._dna_fields(diag))
