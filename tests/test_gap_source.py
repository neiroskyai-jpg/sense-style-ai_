"""Identity Gap на пути без фото считается методом, а не приходит с клиента.

История дефекта: `_quiz_only_diag` брал число из `gap_hint` — JS-эвристики браузера, — не строил
`now_field_distribution` и подделывал `semantic_field_distribution` константой. `_recompute_gap`
выходил до расчёта, и на 922 сохранённых прогонах 910 получили одинаковый 31%, а 813 — доминанту
«классика». Метрика, на которой стоит весь продукт, показывала константу.

Здесь сторожим два свойства: считаем по методу, когда данные это позволяют, и честно признаёмся,
когда нет. Тихая подмена одного другим — то, что и привело к 910 одинаковым замерам.
"""
import pytest

from app.main import _quiz_only_diag

CLASSIC = ["Властная", "Элегантная", "Дорогая"]
ROMANCE = ["Женственная", "Эффектная", "Желанная"]


def _diag(now, want, hint=31, direction="classic"):
    return _quiz_only_diag({"now_traits": now, "want_traits_top3": want}, hint, direction)


def test_gap_is_computed_by_method_when_words_are_calibrated():
    """Все слова из лексикона → считает формула, а подсказка клиента игнорируется."""
    d = _diag(CLASSIC, ROMANCE, hint=31)
    assert d["gap_source"] == "method"
    assert d["gap_percentage"] != 31, "подсказка браузера не должна побеждать расчёт"
    assert d["gap_breakdown"]["field_gap"] == 100.0


def test_scale_discriminates_instead_of_returning_a_constant():
    """Разные ответы — разные числа. Именно этого не было: 910 прогонов с одним значением."""
    values = {
        _diag(CLASSIC, ROMANCE)["gap_percentage"],      # полностью разошлись
        _diag(ROMANCE, ROMANCE)["gap_percentage"],      # совпали
        _diag(["Властная", "Смелая", "Женственная"], ROMANCE)["gap_percentage"],
    }
    assert len(values) == 3, f"шкала не различает состояния: {values}"


def test_identical_points_give_zero_gap():
    """Точка А равна точке Б — разрыва направления нет."""
    assert _diag(ROMANCE, ROMANCE)["gap_percentage"] == 0


def test_dominant_comes_from_the_answers_not_from_the_default():
    """Доминанта берётся из желаемых черт. Раньше при пустом хинте всем ставилась «классика»."""
    assert _diag(CLASSIC, ROMANCE, direction=None)["style_dominant"] == "romance"


def test_distributions_are_real_and_sum_to_hundred():
    d = _diag(CLASSIC, ROMANCE)
    for key in ("now_field_distribution", "semantic_field_distribution"):
        assert round(sum(d[key].values()), 1) == 100.0
    assert "semantic_field_distribution_synthetic" not in d


@pytest.mark.parametrize("now,want", [
    (["спокойная", "надёжная"], CLASSIC),        # половина слов вне лексикона
    (["стильная"], ROMANCE),                     # ни одного опознанного
    ([], ROMANCE),                               # точка А не собрана
])
def test_partial_recognition_never_passes_as_a_method_measurement(now, want):
    """Частичное опознание хуже отказа: «100% классика» из одного слова — уверенность из ничего.

    В таком случае отдаём подсказку клиента, но помечаем и её, и синтетическое распределение.
    """
    d = _diag(now, want, hint=31)
    assert d["gap_source"] == "client_hint"
    assert d["gap_percentage"] == 31
    assert d["semantic_field_distribution_synthetic"] is True
    assert "now_field_distribution" not in d


def test_unknown_words_are_recorded_for_analytics():
    """Слова вне лексикона видны в диагнозе — иначе расхождение метода и квиза не заметить."""
    d = _diag(["спокойная", "деловая"], ROMANCE)
    assert d["lexicon_unknown_words"] == ["спокойная", "деловая"]


def test_broken_hint_falls_back_without_pretending():
    """Негодная подсказка → дефолт 50, но источник по-прежнему честно помечен клиентским."""
    d = _diag(["стильная"], ROMANCE, hint="не число")
    assert d["gap_percentage"] == 50
    assert d["gap_source"] == "client_hint"
