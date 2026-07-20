"""Формула должна читаться в образах, а образы — различаться между собой.

Реальный провал (20.07.2026): клиентке с формулой «Драма × Романтика × Натуральный» собраны
шесть образов, где на всех фото один и тот же длинный плащ хаки. Чистый натуральный, ни драмы,
ни романтики. Плюс процент совпадения показывал 68% у пяти карточек из шести — число выглядело
заглушкой, потому что засчитывало формулу целиком по одному попаданию.
"""
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

LOOK_GENERATOR = io.open("architecture/prompts/look-generator.md", encoding="utf-8").read()

DIAG = {
    "style_formula": "Драма × Романтика × Натуральный",
    "colortype": "autumn_natural",
    "visual_formula": {"silhouettes": ["Полуприлегающий силуэт"],
                       "palette": [{"name": "хаки"}, {"name": "мокко"}]},
}


def _look(items, desc=""):
    return {"items": items, "description": desc, "name": ""}


def test_prompt_requires_every_formula_field_in_the_look():
    assert "ФОРМУЛА ДОЛЖНА ЧИТАТЬСЯ В КАЖДОМ ОБРАЗЕ" in LOOK_GENERATOR
    assert "Драма × Романтика × Натуральный" in LOOK_GENERATOR


def test_prompt_requires_six_different_looks():
    assert "ШЕСТЬ ОБРАЗОВ — ШЕСТЬ РАЗНЫХ ОБРАЗОВ" in LOOK_GENERATOR
    assert "Не больше двух образов на одной базе" in LOOK_GENERATOR


def test_full_formula_scores_higher_than_single_field():
    """Образ, где видно все три поля, обязан обгонять образ с одним «натуральным»."""
    weak = _look(["Плащ хаки", "Брюки палаццо", "Ботильоны", "Сумка-хобо"],
                 "натуральный спокойный образ")
    strong = _look(["Плащ хаки", "Брюки палаццо", "Ботильоны", "Сумка-хобо"],
                   "драма в структуре, романтика в шёлковой блузе, натуральный в фактуре")

    assert m._scenario_formula_match(strong, DIAG, "деловая встреча") > \
        m._scenario_formula_match(weak, DIAG, "деловая встреча")


def test_incomplete_look_scores_lower():
    """Образ без обуви и сумки собран наполовину — процент обязан это показывать."""
    full = _look(["Жакет", "Брюки", "Ботильоны", "Сумка-хобо"])
    partial = _look(["Жакет", "Брюки"])

    assert m._scenario_formula_match(full, DIAG, "деловая встреча") > \
        m._scenario_formula_match(partial, DIAG, "деловая встреча")


def test_score_varies_across_different_looks():
    """Главный симптом: пять карточек из шести с одинаковым числом."""
    looks = [
        _look(["Плащ хаки", "Брюки палаццо", "Ботильоны", "Сумка"], "натуральный"),
        _look(["Шёлковая блуза", "Юбка миди", "Лодочки", "Клатч"], "романтика и драма"),
        _look(["Жакет"], ""),
        _look(["Платье", "Ботильоны", "Сумка-хобо", "Плащ"],
              "драма, романтика, натуральный, полуприлегающий силуэт, хаки"),
    ]
    scores = {m._scenario_formula_match(lk, DIAG, "деловая встреча") for lk in looks}

    assert len(scores) >= 3, scores


def test_capsule_items_come_from_looks_even_without_catalog():
    """Капсула Карты — это разобранные образы, а не подбор из каталога.

    Проверяем без каталога вовсе: если бы капсула бралась оттуда, здесь она была бы пустой.
    """
    looks = [
        {"scenario": "Деловая встреча",
         "items": ["Жакет из тонкой шерсти оливковый", "Брюки палаццо мокко", "Ботильоны"]},
        {"scenario": "Свидание",
         "items": ["Блузка из шёлка кремовая", "Брюки палаццо мокко", "Ботильоны"]},
    ]

    starter = m._core_capsule_from_looks(looks, board=[])
    names = {it["name"].lower() for it in starter}
    from itertools import chain
    in_looks = {i.lower() for i in chain.from_iterable(lk["items"] for lk in looks)}

    assert starter, "капсула не должна быть пустой без каталога"
    assert names <= in_looks, names - in_looks


def test_capsule_item_shows_where_it_works():
    """Сценарии терялись по дороге — и капсула выглядела набором из каталога, хотя им не была."""
    looks = [
        {"scenario": "Деловая встреча", "items": ["Брюки палаццо мокко"]},
        {"scenario": "Свидание", "items": ["Брюки палаццо мокко"]},
    ]

    item = m._core_capsule_from_looks(looks, board=[])[0]

    assert item["scenarios"] == ["Деловая встреча", "Свидание"]
    assert item["outfits_count"] == 2
    assert item["capsule_role"] == "core"


def test_card_plate_states_capsule_origin():
    """Плашка обязана говорить правду: капсула действительно из образов."""
    assert "Собрана из твоих образов" in m.STYLE_CARD
    assert "it.scenarios" in m.STYLE_CARD, "связь со сценариями должна быть на карточке"
