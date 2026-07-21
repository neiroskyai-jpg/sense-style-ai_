"""Формула должна читаться в образах, а образы — различаться между собой.

Реальный провал (20.07.2026): клиентке с формулой «Драма × Романтика × Натуральный» собраны
шесть образов, где на всех фото один и тот же длинный плащ хаки. Чистый натуральный, ни драмы,
ни романтики.

Требование покрыть все поля формулы живёт в промпте — здесь мы стережём именно его. Числовой
метрики совпадения больше нет: считать её было не на чем (см. tests/test_metrics_explainable).
"""
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

LOOK_GENERATOR = io.open("architecture/prompts/look-generator.md", encoding="utf-8").read()

DIAG = {
    "style_formula": "Драма × Романтика × Натуральный",
    "colortype": "autumn_natural",
    "visual_formula": {"silhouettes": ["Полуприлегающий силуэт"],
                       "palette": [{"name": "хаки"}, {"name": "мокко"}]},
}


def _look(items, desc=""):
    return {"items": items, "description": desc, "name": ""}


def test_prompt_requires_every_formula_field_in_the_look():
    assert "ФОРМУЛА ДОЛЖНА ЧИТАТЬСЯ В КАЖДОМ ОБРАЗЕ" in LOOK_GENERATOR
    assert "Драма × Романтика × Натуральный" in LOOK_GENERATOR


def test_prompt_requires_six_different_looks():
    assert "ШЕСТЬ ОБРАЗОВ — ШЕСТЬ РАЗНЫХ ОБРАЗОВ" in LOOK_GENERATOR
    assert "Не больше двух образов на одной базе" in LOOK_GENERATOR


def test_capsule_items_come_from_looks_even_without_catalog():
    """Капсула Карты — это разобранные образы, а не подбор из каталога.

    Проверяем без каталога вовсе: если бы капсула бралась оттуда, здесь она была бы пустой.
    """
    looks = [
        {"scenario": "Деловая встреча",
         "items": ["Жакет из тонкой шерсти оливковый", "Брюки палаццо мокко", "Ботильоны"]},
        {"scenario": "Свидание",
         "items": ["Блузка из шёлка кремовая", "Брюки палаццо мокко", "Ботильоны"]},
    ]

    starter = m._core_capsule_from_looks(looks, board=[])
    names = {it["name"].lower() for it in starter}
    from itertools import chain
    in_looks = {i.lower() for i in chain.from_iterable(lk["items"] for lk in looks)}

    assert starter, "капсула не должна быть пустой без каталога"
    assert names <= in_looks, names - in_looks


def test_capsule_item_shows_where_it_works():
    """Сценарии терялись по дороге — и капсула выглядела набором из каталога, хотя им не была."""
    looks = [
        {"scenario": "Деловая встреча", "items": ["Брюки палаццо мокко"]},
        {"scenario": "Свидание", "items": ["Брюки палаццо мокко"]},
    ]

    item = m._core_capsule_from_looks(looks, board=[])[0]

    assert item["scenarios"] == ["Деловая встреча", "Свидание"]
    assert item["outfits_count"] == 2
    assert item["capsule_role"] == "core"


def test_look_shows_its_own_pieces():
    """Панель капсулы убрана: состав виден прямо в образе, и раскладка рисуется вместе с ним,
    поэтому вещи на ней те самые — а не похожие из каталога."""
    import re

    assert "lk.flatlay" in m.STYLE_CARD
    # Комментарии Jinja в вывод не попадают — ищем только то, что увидит клиентка.
    visible = re.sub(r"\{#.*?#\}", "", m.STYLE_CARD, flags=re.S)
    assert "Опорная капсула" not in visible
