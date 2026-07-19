"""Подбор предметного фото к названию вещи.

Названия приходят из генерации свободным текстом, а библиотека собрана по типам вещей.
Проверяем, что тип узнаётся, узкие типы не перебиваются общими, и что на этом пути нет
обращений к модели: показ картинок должен быть бесплатным.
"""
from core import item_images as ii


def test_type_is_found_in_free_form_names():
    cases = {
        "жакет полуприлегающего силуэта серо-синего цвета": "жакет",
        "ботильоны молочного цвета на устойчивом каблуке": "ботильоны",
        "Структурная сумка-тоут серо-синего цвета": "сумка",
        "блуза из шелка цвета пыльной розы": "блуза",
        "Кожаный плащ миди цвета мягкого какао": "плащ",
        "Брюки прямого кроя каменно-серого цвета": "брюки",
        "Туфли-лодочки оттенка пыльной розы": "туфли",
    }
    for name, kind in cases.items():
        assert ii.item_type(name) == kind, name


def test_narrow_type_wins_over_general_one():
    """«Джинсовая юбка» — юбка, а не джинсы; тренч — не пальто."""
    assert ii.item_type("джинсовая юбка миди") == "юбка"
    assert ii.item_type("классический тренч бежевый") == "тренч"


def test_unknown_name_returns_nothing_instead_of_wrong_picture():
    """Лучше подпись без картинки, чем чужая вещь в карточке."""
    assert ii.item_type("нечто неопознанное") == ""
    assert ii.item_image_url("нечто неопознанное") == ""


def test_url_points_at_existing_file_only():
    """Ссылку отдаём, только если кадр реально лежит на диске."""
    for kind in ii.available_types():
        url = ii.item_image_url(kind)
        assert url == f"{ii.URL_PREFIX}{ii._SLUG[kind]}.jpg", kind


def test_urls_are_latin_only():
    """Кириллица в URL требует процент-кодирования и ломается на части прокси и кэшей."""
    for kind in ii.available_types():
        assert ii.item_image_url(kind).isascii(), kind
