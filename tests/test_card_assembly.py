"""Сборка Карты целиком — на моках модели, без обращений к API.

Дыра в покрытии, из-за которой 19.07.2026 на прод уехала неработающая Карта: тесты не вызывали
build_style_card, и NameError в блоке ДНК всплыл только на живом прогоне. Здесь мы прогоняем
сборку от начала до конца и проверяем, что все обещанные тарифом поля на месте.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

DIAG = {
    "style_formula": "Чистая классика × Минимализм", "gap_percentage": 24,
    "colortype": "winter_natural", "figure_type": "rectangle", "base_style": "classic",
    "primary_substyle": "Деловая классика", "secondary_substyle": "Минимализм",
    "want_traits_top3": ["собранная", "уверенная", "статусная"],
    "tonal_characteristics": {"contrast": "medium"},
    "semantic_field_distribution": {"classic": 60, "natural": 40},
    "visual_formula": {"palette": ["графит", "молочный"], "stop_list": ["неон"],
                       "silhouettes": ["Прямой силуэт", "Длина миди"]},
}


@pytest.fixture
def card(monkeypatch):
    """Карта, собранная без единого обращения к модели."""
    monkeypatch.setattr(m, "generate_capsule", lambda *a, **k: {"looks": [
        {"scenario": s, "items": it, "image_generation_prompt": "x"} for s, it in [
            ("деловая встреча", ["Графитовый жакет", "Прямые брюки", "Лоферы кожаные"]),
            ("презентация", ["Графитовый жакет", "Юбка миди", "Лоферы кожаные"]),
            ("выходные", ["Молочный свитер", "Прямые брюки"]),
            ("свидание", ["Платье-комбинация", "Туфли-лодочки"]),
        ]], "capsule": {"combination_count": 18}})
    monkeypatch.setattr(m, "generate_card_palette", lambda *a, **k: {
        "palette": [{"name": "графит", "hex": "#2F3A46", "group": "base"},
                    {"name": "молочный", "hex": "#EFE7DA", "group": "base"},
                    {"name": "изумруд", "hex": "#0B6E4F", "group": "accent"}],
        "stop_colors": [{"name": "неон", "hex": "#CFFF00", "why": "гасит лицо"}]})
    monkeypatch.setattr(m, "generate_shopping_list", lambda *a, **k: {"items": []})
    monkeypatch.setattr(m, "generate_styling_pair", lambda *a, **k: {})
    monkeypatch.setattr(m, "generate_directions", lambda *a, **k: [])
    monkeypatch.setattr(m, "_inline_capsule_images", lambda board, **k: board)
    return m.build_style_card(DIAG, season="fw")


def test_card_assembles_without_errors(card):
    assert card["formula"] == DIAG["style_formula"]
    assert card["gap"] == 24


def test_card_has_everything_the_tariff_promises(card):
    """Тариф обещает формулу, ДНК, палитру, образы, капсулу-ядро — всё должно быть в Карте."""
    for field in ("formula", "style_dna", "palette", "stop_colors", "looks",
                  "starter_capsule", "combination_count", "substyles", "want_traits"):
        assert card.get(field), f"в Карте нет поля {field}"


def test_capsule_comes_from_looks_not_catalog(card):
    """Каждая вещь капсулы должна встречаться в образах."""
    in_looks = {i.lower() for lk in card["looks"] for i in (lk.get("items") or [])}
    for it in card["starter_capsule"]:
        assert any(w in n for n in in_looks for w in it["name"].lower().split()[:1]), it["name"]


def test_capsule_is_not_tied_to_brand_products(card):
    """Продукт продаётся без договорённостей с брендами: ссылка ведёт на поиск по описанию."""
    for it in card["starter_capsule"]:
        assert "url" not in it, "капсула привязалась к товару из фида"
        assert it.get("search", {}).get("wildberries")


def test_palette_is_grouped(card):
    groups = {p.get("group") for p in card["palette"]}
    assert "base" in groups
