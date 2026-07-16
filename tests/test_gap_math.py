"""Identity Gap считает КОД, а не языковая модель.

Gap — вся метрика продукта: на нём стоит «измеримая трансформация», слайд для жюри и обещание
клиентке. Модель классифицирует черты по семантическим полям (языковая задача), но складывать,
делить и округлять должен код — иначе основание измерительного инструмента вероятностное.

Формула (architecture/prompts/formula-diagnostic.md, v1.1 field-aware):
    field_gap = Σ|want − now| / 2   по 4 полям
    gap       = min(99, round(field_gap + expression_gap)),  expression ∈ {0, 15, 25}
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core.pipeline import _recompute_gap  # noqa: E402


def _diag(now, want, expression=0, llm_gap=None):
    return {
        "gap_percentage": llm_gap,
        "gap_breakdown": {"expression_gap": expression},
        "now_field_distribution": now,
        "semantic_field_distribution": want,
    }


def test_polnoe_sovpadenie_poley_daet_nol():
    """Образ уже читается так, как хочется → разрыва нет."""
    d = {"natural": 25, "romance": 25, "drama": 25, "classic": 25}
    out = _recompute_gap(_diag(d, dict(d), expression=0, llm_gap=0))
    assert out["gap_percentage"] == 0


def test_polya_ne_peresekayutsya_daet_sto_minus_ogranichenie():
    """Профиль впечатления полностью в других полях → 100, но шкала ограничена 99."""
    now = {"natural": 100, "romance": 0, "drama": 0, "classic": 0}
    want = {"natural": 0, "romance": 100, "drama": 0, "classic": 0}
    out = _recompute_gap(_diag(now, want, expression=0))
    assert out["gap_percentage"] == 99, "min(99, ...) обязан ограничивать шкалу"


def test_kontrolnyy_primer_mishel_iz_prompta():
    """Мишель: классика-доминанта → нужны романтика и драма. field_gap ≈ 60, expression 0 → 60%."""
    now = {"classic": 70, "natural": 30, "romance": 0, "drama": 0}
    want = {"classic": 10, "natural": 30, "romance": 30, "drama": 30}
    out = _recompute_gap(_diag(now, want, expression=0))
    assert out["gap_percentage"] == 60
    assert out["gap_breakdown"]["field_gap"] == 60.0


def test_expression_gap_pribavlyaetsya():
    """Надбавка за невыраженность: «прячусь, незаметная» = +25 поверх разрыва полей."""
    now = {"natural": 60, "classic": 40, "romance": 0, "drama": 0}
    want = {"natural": 40, "classic": 40, "romance": 20, "drama": 0}
    out = _recompute_gap(_diag(now, want, expression=25))
    assert out["gap_breakdown"]["field_gap"] == 20.0
    assert out["gap_percentage"] == 45          # 20 + 25


def test_oshibka_modeli_v_arifmetike_ispravlyaetsya_i_fiksiruetsya():
    """Модель посчитала неверно → берём свой расчёт, расхождение записываем как метрику."""
    now = {"classic": 70, "natural": 30, "romance": 0, "drama": 0}
    want = {"classic": 10, "natural": 30, "romance": 30, "drama": 30}
    out = _recompute_gap(_diag(now, want, expression=0, llm_gap=85))   # модель выдумала 85
    assert out["gap_percentage"] == 60, "должен победить расчёт кода"
    assert out["gap_llm_mismatch"]["llm"] == 85
    assert out["gap_llm_mismatch"]["computed"] == 60


def test_nevernaya_stupen_expression_ne_lomaet_schet():
    """Модель отдала произвольную надбавку вместо 0/15/25 — берём 0, а не мусор."""
    now = {"natural": 50, "classic": 50, "romance": 0, "drama": 0}
    want = {"natural": 50, "classic": 50, "romance": 0, "drama": 0}
    out = _recompute_gap(_diag(now, want, expression=37))
    assert out["gap_percentage"] == 0


def test_bitoe_raspredelenie_ne_pereschityvaem_molcha():
    """Сумма долей не 100 — это сигнал сбоя: не «чиним» тихо, а помечаем и не трогаем Gap."""
    now = {"natural": 10, "romance": 10, "drama": 10, "classic": 10}   # сумма 40
    want = {"natural": 25, "romance": 25, "drama": 25, "classic": 25}
    out = _recompute_gap(_diag(now, want, llm_gap=64))
    assert out["gap_percentage"] == 64, "чужое число не трогаем, если данные битые"
    assert "gap_distribution_broken" in out


def test_bez_raspredeleniy_diagnoz_ne_lomaetsya():
    """Старый ответ без распределений (или сбой) — Gap остаётся как был."""
    out = _recompute_gap({"gap_percentage": 50})
    assert out["gap_percentage"] == 50
