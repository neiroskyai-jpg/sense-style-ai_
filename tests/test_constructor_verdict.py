"""Конструктор проверяет правила капсулы на лету, а не просто складывает вещи.

Идея из прототипа фаундера: без вердикта конструктор — коллаж. Клиентка перетаскивает вещи
и не понимает, получился образ или случайный набор. Правила из методологии: один цвет-герой
на образ, законченный комплект (верх+низ либо платье), обувь как завершение.

Логика живёт в браузере, поэтому тест стережёт её присутствие и формулировки: их видит
клиентка, и они не должны потеряться при правках шаблона.
"""
import os
import re

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402


def test_verdict_block_exists():
    assert 'id=verdict' in m.CABINET_PAGE
    assert "renderVerdict" in m.CABINET_PAGE


def test_one_hero_colour_rule_is_enforced():
    """Главное правило капсулы: два ярких цвета в образе спорят между собой."""
    assert "оставь один цвет-герой" in m.CABINET_PAGE
    assert "ACCENT_WORDS" in m.CABINET_PAGE


def test_accent_words_cover_real_palette_colours():
    """Список акцентов должен покрывать цвета, которыми реально названы вещи в капсуле."""
    block = re.search(r"var ACCENT_WORDS = \[(.*?)\];", m.CABINET_PAGE, re.S).group(1)

    for colour in ("изумруд", "бордов", "красн", "фукси", "горчич"):
        assert colour in block, colour


def test_incomplete_outfit_is_named_incomplete():
    """Верх без низа — не образ, и клиентка должна это увидеть до того, как соберёт неделю."""
    assert "иначе образ не закончен" in m.CABINET_PAGE
    assert "Добавь обувь" in m.CABINET_PAGE


def test_dress_counts_as_complete_base():
    """Платье — самостоятельная основа: требовать к нему верх и низ нельзя."""
    assert "Платья и комбинезоны" in m.CABINET_PAGE
    assert "has('Платья и комбинезоны') || (has('Верх') && has('Низ'))" in m.CABINET_PAGE


def test_every_item_shows_how_many_outfits_it_gives():
    """Правило капсулы: вещь, работающая меньше чем в трёх комплектах, — не опора.

    Число на карточке показывает ЦЕННОСТЬ вещи, а не только её вид: клиентка видит, что
    брюки собирают шесть образов, а платье — один, и понимает, во что вкладываться.
    """
    board = [
        {"slot": "Верх", "items": [{"name": "Блузка"}, {"name": "Топ"}]},
        {"slot": "Низ", "items": [{"name": "Брюки"}, {"name": "Юбка"}]},
        {"slot": "Обувь", "items": [{"name": "Лоферы"}]},
        {"slot": "Верхний слой", "items": [{"name": "Жакет"}]},
    ]

    counts = m.outfits_per_item(board)

    assert counts["Блузка"] == 2, "верх работает с каждым низом"
    assert counts["Лоферы"] == 4, "обувь входит в каждый комплект"
    assert counts["Жакет"] == 4, "слой дополняет любой комплект"


def test_dress_counts_only_with_shoes():
    """Платье — готовый образ, ему нужна только обувь, верх с низом не нужны."""
    board = [
        {"slot": "Платья и комбинезоны", "items": [{"name": "Платье миди"}]},
        {"slot": "Обувь", "items": [{"name": "Лоферы"}, {"name": "Ботильоны"}]},
    ]

    assert m.outfits_per_item(board)["Платье миди"] == 2


def test_counter_is_rendered_and_flags_weak_items():
    assert "combos_per_item" in m.CABINET_PAGE
    assert "pcombos" in m.CABINET_PAGE
    assert "n < 3" in m.CABINET_PAGE, "вещь слабее трёх комплектов должна помечаться"


def test_week_outfit_can_be_loaded_into_constructor():
    """«Собрать этот образ»: готовый день дня переносится в конструктор одним кликом.

    Без этого связь «вот образы» и «собери свой» терялась — клиентка смотрела план недели,
    а сборку начинала с нуля.
    """
    assert "wdtake" in m.CABINET_PAGE
    assert "Собрать этот образ" in m.CABINET_PAGE
    # вещь ищется среди карточек капсулы по имени: так в ячейку попадает та же вещь с фото
    assert "data-name" in m.CABINET_PAGE and "querySelectorAll('.pitem')" in m.CABINET_PAGE
