"""Identity Gap считает КОД, а не языковая модель.

Gap — вся метрика продукта: на нём стоит «измеримая трансформация», слайд для жюри и обещание
клиентке. Модель классифицирует черты по семантическим полям (языковая задача), но складывать,
делить и округлять должен код — иначе основание измерительного инструмента вероятностное.

Формула (architecture/prompts/formula-diagnostic.md, v1.1 field-aware):
    field_gap = Σ|want − now| / 2   по 4 полям
    gap       = min(99, round(field_gap + expression_gap)),  expression ∈ {0, 15, 25}

Надбавка вне ступеней, битые распределения и отсутствие данных не «чинятся» молча: каждый
случай помечается в диагнозе (expression_gap_defaulted / gap_distribution_broken / gap_source),
иначе сбой измерения неотличим от честного нуля.
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


def test_kontrolnyy_primer_anna_iz_prompta():
    """Анна: почти на месте по направлению (классика и сейчас, и в желании), тормозит
    невыраженность. field_gap = 26, expression +15 («спорт-база, ношу одно и то же» —
    дословно определение ступени +15) → 41%. Раньше пример стоял +20 → 46%, но +20 не входит
    в шкалу 0/15/25 и код молча обнулял бы надбавку, давая 26% вместо 41."""
    now = {"classic": 55, "natural": 35, "drama": 10, "romance": 0}
    want = {"classic": 53, "drama": 23, "romance": 13, "natural": 11}
    out = _recompute_gap(_diag(now, want, expression=15))
    assert out["gap_breakdown"]["field_gap"] == 26.0
    assert out["gap_percentage"] == 41


def test_nevernaya_stupen_expression_ne_lomaet_schet_i_fiksiruetsya():
    """Надбавка вне 0/15/25 → берём 0, но помечаем подмену, а не глотаем молча.

    expression до 25 — четверть шкалы. Тихое обнуление не отличить от честного 0, поэтому
    подмену пишем в диагноз: у 6 из 12 старых прогонов надбавка была вне ступеней."""
    now = {"natural": 50, "classic": 50, "romance": 0, "drama": 0}
    want = {"natural": 50, "classic": 50, "romance": 0, "drama": 0}
    out = _recompute_gap(_diag(now, want, expression=37))
    assert out["gap_percentage"] == 0
    assert out["expression_gap_defaulted"] == {"raw": 37, "used": 0}


def test_bitoe_raspredelenie_snimaet_dogadku_modeli():
    """Сумма долей не 100 — считать нечем. Раньше здесь оставалось число модели (то самое
    «основание метрики на LLM», что запрещает докстринг). Теперь Gap снимаем и помечаем неполным."""
    now = {"natural": 10, "romance": 10, "drama": 10, "classic": 10}   # сумма 40
    want = {"natural": 25, "romance": 25, "drama": 25, "classic": 25}
    out = _recompute_gap(_diag(now, want, llm_gap=64))
    assert out["gap_percentage"] is None, "догадку модели не выдаём за расчёт"
    assert out["gap_source"] == "unavailable"
    assert "gap_distribution_broken" in out


def test_bez_raspredeleniy_diagnoz_ne_lomaetsya():
    """Старый ответ без распределений (или сбой) — Gap остаётся как был."""
    out = _recompute_gap({"gap_percentage": 50})
    assert out["gap_percentage"] == 50
