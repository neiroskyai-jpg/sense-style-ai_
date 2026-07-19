"""Направление — регистр прочтения Формулы, а не новый стиль.

Фаундер, 19.07.2026: «что за такие названия откуда?» — на экране результата квиза стояли
«Нежная Реформаторская» и «Структурный Романтизм». В методе 4 стиля и 25 подстилей, таких
названий там нет. Клиентка только что получила свою Формулу, и чужой ярлык рядом с ней
читается как вторая, другая диагностика.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core.pipeline import _canonical_direction_names as canon  # noqa: E402

DIAG = {
    "style_formula": "Классика × Драма-акцент",
    "primary_substyle": "Soft Classic",
    "secondary_substyle": "Драма-акцент",
}


def test_invented_style_labels_are_replaced():
    out = canon([{"name": "Нежная Реформаторская"}, {"name": "Структурный Романтизм"}], DIAG)

    assert [d["name"] for d in out] == ["Мягкая версия", "Собранная версия"]


def test_register_names_are_kept():
    """«Мягкая версия», «Тихое прочтение» — это регистр, а не выдуманный стиль."""
    out = canon([{"name": "Мягкая версия"}, {"name": "Тихое прочтение"}], DIAG)

    assert [d["name"] for d in out] == ["Мягкая версия", "Тихое прочтение"]


def test_her_own_substyles_are_kept():
    """Подстиль из её Формулы — не выдумка, его оставляем."""
    out = canon([{"name": "Soft Classic"}, {"name": "Драма-акцент"}], DIAG)

    assert [d["name"] for d in out] == ["Soft Classic", "Драма-акцент"]


def test_empty_name_gets_a_canonical_one():
    out = canon([{"name": ""}, {}], DIAG)

    assert [d["name"] for d in out] == ["Мягкая версия", "Собранная версия"]


def test_other_fields_are_untouched():
    """Чиним только ярлык: состав образа и промпт рендера трогать нельзя."""
    src = [{"name": "Мягкий Авангард", "items": ["жакет"], "image_generation_prompt": "p"}]

    out = canon(src, DIAG)

    assert out[0]["items"] == ["жакет"] and out[0]["image_generation_prompt"] == "p"
