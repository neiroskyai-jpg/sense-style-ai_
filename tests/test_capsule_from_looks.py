"""Капсула вырастает из образов, а не подбирается из каталога.

Логика продукта, объяснённая фаундером: ИИ собирает образы под формулу и цветотип, из ИХ вещей
складывается капсула, а уже из капсулы клиентка собирает свои комплекты в конструкторе.

Раньше капсула была списком карточек из каталога: вещи подбирались похожие, не те, и блок спорил
с образами — «на фото одно, в капсуле другое». Теперь это раскладка тех же вещей.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402
from core import pipeline as p  # noqa: E402

LOOKS = [
    {"items": ["Жакет молочный", "Водолазка чёрная", "Брюки молочные", "Лодочки чёрные"]},
    {"items": ["Жакет молочный", "Юбка миди", "Лодочки чёрные", "Сумка чёрная"]},
    {"items": ["Брюки молочные", "Рубашка белая"]},
]


def test_capsule_is_built_from_look_items():
    names = m.capsule_items_from_looks(LOOKS)

    assert "Жакет молочный" in names
    assert all(any(n in (lk["items"]) for lk in LOOKS) for n in names), \
        "в капсуле не может быть вещи, которой нет ни в одном образе"


def test_repeating_items_come_first():
    """Вещь, работающая в нескольких образах, — опора гардероба, она идёт первой."""
    names = m.capsule_items_from_looks(LOOKS)

    assert names[0] in ("Жакет молочный", "Брюки молочные", "Лодочки чёрные")
    assert names.index("Жакет молочный") < names.index("Рубашка белая")


def test_capsule_size_is_capped():
    many = [{"items": [f"Вещь {i}" for i in range(30)]}]

    assert len(m.capsule_items_from_looks(many, limit=12)) == 12


def test_underwear_never_enters_the_capsule():
    """Фиды отдают бельё вперемешку с одеждой; в капсуле его быть не может."""
    names = m.capsule_items_from_looks([{"items": ["Топ-бра", "Жакет"]}])

    assert names == ["Жакет"]


def test_capsule_flatlay_is_a_style_book_spread():
    """Капсула — разворот стайл-бука: вещи рядами, без людей и текста."""
    seen = {}

    def fake(instruction, model=None, ref_images=None):
        seen["p"] = instruction
        return ["data:img"]

    orig = p.provider.generate_image
    p.provider.generate_image = fake
    try:
        p.render_capsule_flatlay(["Жакет молочный", "Брюки молочные"])
    finally:
        p.provider.generate_image = orig

    low = seen["p"].lower()
    assert "rows" in low and "style-book" in low
    assert "no people" in low and "no text" in low


def test_card_shows_capsule_as_one_layout():
    assert "c.capsule_flatlay" in m.STYLE_CARD
    assert "Собрана из образов выше" in m.STYLE_CARD
