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


def test_summer_render_forbids_winter_pieces():
    """Летний образ без пальто, меха и бархата — иначе капсулу невозможно надеть по погоде."""
    canon = p._season_canon("summer").lower()

    assert "no coats" in canon and "no fur" in canon and "no velvet" in canon
    assert "linen" in canon and "sandals" in canon
    assert p._season_canon("spring") == p._season_canon("summer")


def test_winter_render_requires_outerwear():
    canon = p._season_canon("winter").lower()

    assert "outerwear required" in canon
    assert "no bare arms" in canon and "no sandals" in canon
    assert p._season_canon("autumn") == p._season_canon("winter")


def test_unknown_season_stays_silent():
    """Сезона нет — молчим, а не выдумываем: лучше без указания, чем не тот."""
    assert p._season_canon(None) == ""
    assert p._season_canon("осень-зима") == ""


def test_season_reaches_the_rendered_instruction(monkeypatch):
    """Сезон обязан доезжать до самой картинки, а не оставаться в текстовом промпте."""
    seen = {}

    def fake_generate_image(instruction, model=None, ref_images=None):
        seen["instruction"] = instruction
        return ["data:image/png;base64,AA=="]

    monkeypatch.setattr(p.provider, "generate_image", fake_generate_image)
    monkeypatch.setattr(p.provider, "encode_image", lambda *a, **k: "BODY")
    monkeypatch.setattr(p.provider, "head_crop", lambda *a, **k: "FACE")

    p.render_look_on_client("photo.jpg", "linen dress", season="summer")

    assert "SEASON: spring/summer" in seen["instruction"]
