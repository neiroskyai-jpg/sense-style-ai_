"""Состав образа и картинка образа — про одно и то же.

Жалоба фаундера (четвёртый заход на «капсулу из образов»): на фото вязаный кардиган и платье,
а в опорной капсуле под ним — жакет, брюки и лоферы. Капсула выглядит взятой с потолка.

Причина была не в сборке капсулы: она честно строится из looks[].items. Расходились сами данные —
промпт просил `items` и `image_generation_prompt` как два независимых поля, ничем их не связывая.
Модель писала список одних вещей, а картинку заказывала по другим.
"""
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402

LOOK_GENERATOR = io.open("architecture/prompts/look-generator.md", encoding="utf-8").read()


def test_directions_prompt_binds_image_to_items():
    """Два образа после квиза — витрина продукта: расхождение видно сразу."""
    assert "ПЕРЕЧИСЛИ РОВНО ТЕ ЖЕ ВЕЩИ, что в items" in p._DIRECTIONS_SYSTEM


def test_look_generator_binds_image_to_items():
    """Шесть образов Карты — там же, откуда собирается опорная капсула."""
    assert "ПЕРЕЧИСЛИ РОВНО ТЕ ЖЕ ВЕЩИ, что в items" in LOOK_GENERATOR


def test_directions_prompt_bans_dated_pieces():
    """Вязаный кардиган-оверсайз пришёл именно отсюда — модель не знала, что он немодный."""
    prompt = p._DIRECTIONS_SYSTEM.lower()

    for dated in ("вязаный кардиган-оверсайз", "рукав 3/4", "скинни", "бананка"):
        assert dated in prompt, dated


def test_directions_prompt_requires_capsule_logic():
    """Два образа — начало капсулы: вещи обязаны сочетаться между собой, иначе носить нечего."""
    prompt = p._DIRECTIONS_SYSTEM

    assert "низ из первого обязан работать с верхом из второго" in prompt
    assert "ЭТО ВИТРИНА ПРОДУКТА" in prompt
