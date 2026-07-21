"""Блок «Одна вещь — два образа»: приём должен быть показан, а не только заявлен.

Разбор фаундера по реальной паре с прода:
- якорем назвали брюки, а держал оба образа красный акцент — сами брюки не читались;
- на двух кадрах были РАЗНЫЕ женщины, и идея «гардероб одного человека» рассыпалась;
- «Деловой авангард» стояло над классическим костюмом с акцентом.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402


def test_anchor_must_be_visible():
    """Приём «одна вещь» работает, только когда за якорем следит глаз."""
    assert "ЯКОРЬ ВИДЕН" in p._STYLING_SYSTEM
    assert "растворяться тёмным пятном" in p._STYLING_SYSTEM


def test_name_must_match_content():
    """Ярлык, который больше картинки, стилист и клиентка считывают сразу."""
    assert "НАЗВАНИЕ = СОДЕРЖАНИЕ" in p._STYLING_SYSTEM
    assert "авангард" in p._STYLING_SYSTEM


def test_description_names_the_anchor():
    """Без явного «общее — эти брюки, меняется верх» остаются просто два образа."""
    assert "общее — эти брюки" in p._STYLING_SYSTEM


def test_appearance_is_never_described_in_image_prompt():
    """Главная причина разных лиц: модель дописывала внешность, и рендер брал чужую вместо фото."""
    assert "БЕЗ ОПИСАНИЯ ЛИЦА" in p._STYLING_SYSTEM
    assert "человек ОДИН" in p._STYLING_SYSTEM


def test_season_consistency_between_the_pair():
    """Босоножки к шерстяному костюму — видимый конфликт."""
    assert "Сезон и обувь держи согласованными" in p._STYLING_SYSTEM
