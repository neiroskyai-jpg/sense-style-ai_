"""Валидность экспертного датасета для eval (без обращения к API).

Датасет — основа DS-метрик конкурса. Тест охраняет его целостность: если кейс потеряет
разметку или поле, метрики молча поедут.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from evaluation.eval_diagnosis import _dominant, _formula_hit, _quiz, load_cases  # noqa: E402

_FIELDS = {"classic", "natural", "drama", "romance"}


def test_dataset_has_expert_labels():
    cases = load_cases()
    assert len(cases) >= 3
    for c in cases:
        assert c["now_traits"], c["id"]
        assert c["want_traits"], c["id"]
        assert len(c["want_traits_top3"]) == 3, c["id"]
        assert c["expert_dominant_field"], c["id"]
        assert set(f.lower() for f in c["expert_dominant_field"]) <= _FIELDS, c["id"]
        assert c["expert_figure"] and c["expert_colortype"], c["id"]
        assert c["expert_formula_keywords"], c["id"]


def test_quiz_input_uses_top3_by_default_and_full_on_ablation():
    case = load_cases()[0]
    assert _quiz(case)["want_traits_top3"] == case["want_traits_top3"]          # как в проде
    assert _quiz(case, full_want=True)["want_traits_top3"] == case["want_traits"]  # ablation


def test_dominant_picks_max_field():
    assert _dominant({"classic": 0.2, "romance": 0.7, "drama": 0.1}) == "romance"
    assert _dominant({}) is None
    assert _dominant(None) is None


def test_formula_hit_is_case_insensitive_substring():
    assert _formula_hit("Наивный романтизм × Минимализм", ["романт"]) is True
    assert _formula_hit("Чистая классика × Quiet Luxury", ["романт", "old money"]) is False
    assert _formula_hit(None, ["романт"]) is False
