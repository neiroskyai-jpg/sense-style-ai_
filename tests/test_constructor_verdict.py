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
