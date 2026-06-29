"""Тесты RAG-ретривера — на закоммиченном индексе, без API и без тяжёлых пакетов.

Проверяем именно tag-путь (точное совпадение цветотип/фигура/поле) — он же работает
в CI и в проде без fastembed/numpy. Семантика — опциональный слой поверх.
"""
from core import rag


def _retrieve(**profile):
    return rag.retrieve(profile, k=6)


def test_index_present():
    assert len(rag._chunks()) > 50, "индекс чанков не собран — запусти build_rag_index"


def test_retrieve_surfaces_clients_colortype_and_figure():
    rules = _retrieve(
        colortype="winter_natural", figure_type="rectangle", base_style="mixed",
        primary_substyle="minimalism",
        style_formula="Минимализм × Power Woman",
        want_traits_top3=["властная", "элегантная"],
        semantic_field_distribution={"natural": 0, "romance": 25, "drama": 38, "classic": 37},
    )
    assert rules, "ретривер ничего не вернул"
    # её цветотип должен сработать
    assert any("winter_natural" in (r["matched"].get("colortype") or []) for r in rules)
    # её фигура должна сработать
    assert any("rectangle" in (r["matched"].get("figure") or []) for r in rules)


def test_no_cross_figure_false_match():
    """Чанк про «прямоугольник» не должен матчиться на грушу (теги по заголовку секции)."""
    rules = _retrieve(colortype="autumn_natural", figure_type="pear",
                      semantic_field_distribution={"romance": 50, "classic": 25})
    for r in rules:
        if "figure" in r["matched"]:
            assert r["matched"]["figure"] == ["pear"], f"ложный тег фигуры: {r['matched']}"


def test_cited_rules_shape():
    rules = _retrieve(colortype="summer_light", figure_type="hourglass")
    cited = rag.cited_rules(rules)
    assert cited and all({"label", "section", "snippet"} <= set(c) for c in cited)


def test_empty_profile_does_not_crash():
    assert rag.retrieve({}, k=6) == [] or isinstance(rag.retrieve({}, k=6), list)
