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


def test_flatlay_prompt_has_no_people_and_no_text():
    """Раскладка — это вещи, а не съёмка на модели. И без рекламного текста, из-за которого
    пришлось отказаться от маркетплейсных фото."""
    seen = {}

    def fake(instruction, model=None, ref_images=None):
        seen["p"] = instruction
        return ["data:img"]

    orig = p.provider.generate_image
    p.provider.generate_image = fake
    try:
        p.render_flatlay(["прямые брюки", "жакет"], palette="graphite")
    finally:
        p.provider.generate_image = orig

    low = seen["p"].lower()
    assert "no people" in low and "no faces" in low
    assert "no text" in low and "no logos" in low
    assert "top-down" in low, "раскладка снимается строго сверху"


def test_flatlay_lays_trousers_full_length():
    """Правка фаундера: брюки не «сложены», а разложены ровно во всю длину — как жакет."""
    seen = {}

    def fake(instruction, model=None, ref_images=None):
        seen["p"] = instruction
        return ["data:img"]

    orig = p.provider.generate_image
    p.provider.generate_image = fake
    try:
        p.render_flatlay(["брюки"])
    finally:
        p.provider.generate_image = orig

    assert "fully extended to full length" in seen["p"]


def test_flatlay_needs_no_client_photo():
    """В кадре нет человека, поэтому identity-рендер и фото клиентки не нужны."""
    assert p.render_flatlay([]) == ""


def test_flatlay_model_is_separate_from_identity_render():
    """Раскладку можно менять свободно: людей в кадре нет, и эксперимент с моделью не задевает
    персональные образы, где Gemini единственный держит лицо клиентки (GPT отказывает)."""
    from core import config

    assert config.MODELS["image"]["flatlay"] != "" 
    assert "gemini" in config.MODELS["image"]["dressing"], \
        "identity-рендер обязан остаться на Gemini: OpenAI отказывается воссоздавать реальные лица"


def test_no_dotted_items_access_in_templates():
    """`x.items` в Jinja резолвится в МЕТОД словаря, а не в список вещей.

    Наступали дважды: сначала matrix.items выводил «<built-in method items of dict>» в Карту,
    потом lk.items|join уронил всю страницу в 500 («builtin_function_or_method is not iterable»).
    Доступ к ключу «items» — только через квадратные скобки.
    """
    import io
    import re

    app_src = io.open("app/main.py", encoding="utf-8").read()
    bad = re.findall(r"\{\{[^}]*\b\w+\.items\b[^}]*\}\}", app_src)
    bad += re.findall(r"\{%[^%]*\b\w+\.items\b[^%]*%\}", app_src)

    assert not bad, f"точечный доступ к .items в шаблоне: {bad[:3]}"
