"""Качество капсулы: вещи должны читаться как Формула клиентки, а не как выдача каталога.

Разбор фаундера 18.07.2026: на «Классику 60 / Натуральность 40» в мягкой палитре движок выдавал
чёрное платье на бретелях, две пары кроссовок, летний лён в капсуле «осень–зима» и пальто в слоте
«Аксессуары». Каждый дефект имел свою причину — они и зафиксированы здесь.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402
from core.catalog import Product, score_products  # noqa: E402

CARD = {
    "formula": "Классика 60 / Натуральность 40", "season": "fw",
    "palette": [{"name": "глубокий хвойный"}, {"name": "серо-зелёный"},
                {"name": "молочный"}, {"name": "графитовый"}],
    "stop_colors": [{"name": "неон"}],
}
DIAG = {"figure_type": "rectangle", "base_style": "classic",
        "semantic_field_distribution": {"classic": 60, "natural": 40}}


def test_stop_colors_reach_the_filter():
    """Карта хранит табу в `stop_colors`, а фильтр читал `stop_list` — табу не доезжали вовсе.

    Это и был источник чёрного платья в капсуле мягкого лета.
    """
    products = [
        Product(id="1", name="Платье чёрное на бретелях", category="платье", color="чёрный"),
        Product(id="2", name="Жакет приталенный", category="жакет", color="хвойный"),
    ]
    profile = {"palette": [{"name": "хвойный"}], "stop_list": [{"name": "чёрный"}],
               "base_style": "classic", "gender": "женский"}
    names = [p.name for _, p in score_products(profile, products)]
    assert "Платье чёрное на бретелях" not in names
    assert "Жакет приталенный" in names


def test_slot_is_decided_by_earliest_word_not_list_order():
    """«Пальто … с поясом» — это пальто, а не пояс."""
    assert m._capsule_slot("Пальто свободное демисезонное с поясом") == "Верхний слой"
    assert m._capsule_slot("Ремень кожаный с пряжкой") == "Аксессуары"


def test_item_name_beats_lying_feed_category():
    """Категория фида врёт: у пальто WB она приходит как «аксессуар». Верим названию."""
    assert m._capsule_slot("аксессуар") == "Аксессуары"           # сама категория читается так
    assert m._capsule_slot("Пальто с поясом", "аксессуар") == "Верхний слой"


@pytest.mark.parametrize("name,season,ok", [
    ("Рубашка летняя из льна", "fw", False),
    ("Водолазка шерстяная", "fw", True),
    ("Пуховик тёплый", "ss", False),
    ("Рубашка хлопковая", "ss", True),
])
def test_season_filter(name, season, ok):
    """Капсула собирается на сезон, а каталог о сезоне не знает — фильтруем по названию."""
    assert m._season_ok(name, season) is ok


def test_beachwear_is_not_capsule():
    """«Рубашка летняя прозрачная для пляжа» приходила в рабочую капсулу как обычный верх."""
    assert m._is_capsule_worthy("Рубашка летняя прозрачная для пляжа") is False
    assert m._is_capsule_worthy("Рубашка с карманами из фланели") is True


def test_sporty_shoes_are_not_capsule_base():
    assert m._is_sporty_shoe("Кроссовки 1 форсы") is True
    assert m._is_sporty_shoe("Лоферы из натуральной кожи") is False


@pytest.mark.skipif(not os.path.exists("data/fashion-base/products_wb.csv"),
                    reason="нет каталога в этом окружении")
def test_real_capsule_reads_as_classic():
    """Сквозная проверка на реальном каталоге: капсула классики — без кроссовок и не летняя."""
    board = m._visual_capsule(CARD, DIAG, 12)
    assert board, "капсула не собралась"
    by_slot = {g["slot"]: [i["name"] for i in g["items"]] for g in board}

    # Обуви в капсуле нет и не должно быть: в брендовых фидах со студийной съёмкой её нет вовсе,
    # а маркетплейсная приходила с рекламным текстом поверх фото («ТРЕНД 2026», логотипы) и
    # уггами в летней капсуле. Обувь называется в составе образа текстом — без чужого фото.
    # Но если она когда-нибудь появится, спортивной в капсуле классики быть всё равно нельзя.
    shoes = by_slot.get("Обувь") or []
    assert not any(m._is_sporty_shoe(s) for s in shoes), f"кроссовки в капсуле классики: {shoes}"

    all_names = [n for names in by_slot.values() for n in names]
    assert not any(m._season_ok(n, "fw") is False for n in all_names), \
        f"летняя вещь в капсуле осень–зима: {all_names}"
    # пальто/тренч не должны оказаться среди сумок и ремней
    assert not any("пальто" in n.lower() for n in (by_slot.get("Аксессуары") or []))


def test_starter_capsule_is_exactly_nine():
    """Тариф обещает стартовую капсулу из 9 вещей — столько и должно приходить.

    Ступени 9 не было: любое n>6 давало 12, и клиентка за 3900 ₽ получала не то число,
    что прочитала на лендинге.
    """
    assert sum(m._capsule_quota(9).values()) == 9
    assert sum(m._capsule_quota(6).values()) == 6
    assert sum(m._capsule_quota(12).values()) == 12


def test_tops_outnumber_bottoms_in_every_size():
    """Канон «Алгоритмы имиджа»: капсула богатеет за счёт верхов."""
    for n in (6, 9, 12):
        q = m._capsule_quota(n)
        assert q["Верх"] > q["Низ"], f"n={n}: верхов не больше, чем низов"
