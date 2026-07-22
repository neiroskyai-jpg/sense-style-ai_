"""Стили должны доезжать до генерации содержанием, а не ярлыком.

До этого модуля в промпт ехали 25 подстилей маркерами и 4 чистых стиля атрибутами. Самих
коммерческих и исторических стилей — милитари, сафари, гаучо, рустика, авангарда — не было ни
в каком виде: модель видела слово и наполняла его как умела. Здесь проверяем, что справочник
модуля 4 курса доезжает и что правила микса едут всегда.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from core.canon import substyles  # noqa: E402
from core.style_cards import known_substyles, style_cards_prompt  # noqa: E402


def test_card_is_picked_by_client_substyle():
    """Карточка едет под Формулу клиентки, чужие — нет: справочник большой, весь не влезает."""
    prompt = style_cards_prompt("Преппи")

    assert "### Преппи" in prompt
    assert "ромб-аргайл" in prompt, "принты стиля — то, ради чего карточка и нужна"
    assert "### Милитари" not in prompt, "чужой стиль в промпт не едет"


def test_mix_rules_travel_even_without_a_matching_card():
    """Правила прочтения и запреты не зависят от Формулы — они едут всегда.

    Их нарушают чаще всего: перестилизация и цитирование «в лоб» портят образ независимо от
    того, есть ли для подстиля клиентки отдельная карточка.
    """
    prompt = style_cards_prompt("Чистая классика")   # карточки под неё в модуле 4 нет

    assert "### " not in prompt, "карточек нет — но блок не пустой"
    assert "максимум 3" in prompt.lower() or "точечно" in prompt
    assert "Авангард не сочетается с романтикой" in prompt


def test_hard_prohibitions_are_present():
    """Пары, которые не смешиваются. Без них модель миксует рустик с драмой и считает смелостью."""
    prompt = style_cards_prompt("Рустикальный")

    assert "Не сочетается" in prompt, "запрет живёт в самой карточке рустика"
    assert "драма" in prompt.lower()


@pytest.mark.parametrize("name", sorted(known_substyles()))
def test_every_card_maps_to_a_canonical_substyle(name):
    """Карточка не имеет права ссылаться на выдуманный подстиль: в методе их ровно 25."""
    assert name in substyles(), f"{name} — не канонический подстиль метода"


def test_cards_cover_the_styles_the_course_module_describes():
    """Модуль 4 закрывает как раз те подстили, которые справочник сам называл «тонкими»."""
    covered = known_substyles()

    for name in ("Преппи", "Денди-Гарсон", "Бохо и бохо-шик", "Рустикальный",
                 "Авангард", "Минимализм", "Спорт-шик", "Чистый натуральный"):
        assert name in covered, name


def test_capsule_prompt_carries_style_cards(monkeypatch):
    """Сборка капсулы обязана нести карточки — иначе весь справочник лежит мёртвым грузом."""
    from core import pipeline, provider

    seen = {}

    def _fake(model, system, user, **kw):
        seen["system"] = system
        return {"capsule": [], "looks": []}

    monkeypatch.setattr(provider, "chat_json", _fake)
    pipeline.generate_capsule(
        {"style_formula": "Преппи × Минимализм", "primary_substyle": "Преппи",
         "secondary_substyle": "Минимализм", "figure_type": "hourglass"},
        {"scenarios": ["работа"]},
    )

    assert "ромб-аргайл" in seen["system"], "карточка преппи"
    assert "Авангард не сочетается" in seen["system"], "запреты микса"


def test_directions_prompt_carries_style_cards(monkeypatch):
    """Направление задаёт стиль всей выдачи — значит состав стиля нужен уже здесь."""
    from core import pipeline, provider

    seen = {}

    def _fake(model, system, user, **kw):
        seen["system"] = system
        return {"directions": []}

    monkeypatch.setattr(provider, "chat_json", _fake)
    pipeline.generate_directions({"style_formula": "Милитари", "primary_substyle": "Чистый натуральный"})

    assert "жилет Вассермана" in seen["system"], "состав милитари, а не один ярлык"
