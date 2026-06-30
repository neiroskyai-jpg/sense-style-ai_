"""Тест гарантии «ровно 6 образов» в «Карте стиля» — без API.

LLM капсулы иногда отдаёт меньше образов, чем сценариев. `_ensure_n_looks` должен
всегда вернуть по одному образу на каждый сценарий (переназначение + досборка).
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")  # импорт app.main не должен падать

from app.main import _ensure_n_looks  # noqa: E402

SCENARIOS = ["работа", "деловая встреча", "повседневное",
             "событие и выход", "свидание", "путешествие"]
CAPSULE = {"capsule": {"items": [{"name": "жакет"}, {"name": "брюки"}, {"name": "пальто"}]}}
DIAG = {"style_formula": "Классика", "visual_formula": {"silhouettes": ["жакет", "юбка"]}}


def test_pads_missing_scenario_to_six():
    looks = [{"scenario": s, "items": ["x"]} for s in SCENARIOS[:5]]  # нет 'путешествие'
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert [o["scenario"] for o in out] == SCENARIOS
    trip = next(o for o in out if o["scenario"] == "путешествие")
    assert trip["items"]  # дособран из капсулы, не пустой


def test_reassigns_extras_and_synthesizes():
    looks = [{"items": ["x"]}, {"items": ["y"]}]  # без сценариев
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert len({o["scenario"] for o in out}) == 6  # все сценарии уникальны


def test_empty_looks_still_six():
    out = _ensure_n_looks([], SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert all(o["items"] for o in out)


def test_no_duplicate_scenario_assignment():
    # два образа с одним сценарием — второй уходит в extras и переназначается
    looks = [{"scenario": "работа", "items": ["a"]}, {"scenario": "работа", "items": ["b"]}]
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6 and len({o["scenario"] for o in out}) == 6
