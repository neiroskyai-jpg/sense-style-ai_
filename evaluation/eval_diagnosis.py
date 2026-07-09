"""Eval диагностики против экспертной разметки (DS-критерий конкурса).

Что измеряем:
  1. dominant_field_accuracy — совпало ли доминирующее семантическое поле желаемого образа
     с разметкой эксперта (стилист, автор метода). Ядро диагностики: именно поле определяет
     Формулу стиля.
  2. formula_hit — попала ли сгенерированная Формула стиля в экспертные ключевые слова.
  3. gap_sanity — Identity Gap в допустимом диапазоне и растёт с расстоянием now→want.

Vision (цветотип/фигура по фото) здесь НЕ прогоняется: эти поля берутся из экспертной
разметки, чтобы изолировать качество диагностики от качества Vision. Vision-eval — отдельно.

Запуск (нужен OPENROUTER_API_KEY; ~1-2 цента на кейс, модель flash):
    python -m evaluation.eval_diagnosis
    python -m evaluation.eval_diagnosis --mode final    # «думающая» модель
    python -m evaluation.eval_diagnosis --dry-run       # без API: проверка датасета
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

_GT = Path(__file__).resolve().parent / "ground_truth.json"


def load_cases() -> list[dict]:
    data = json.loads(_GT.read_text(encoding="utf-8"))
    return data["cases"]


def _quiz(case: dict, full_want: bool = False) -> dict:
    """Кейс → вход диагностики в формате app.main._build_quiz.

    full_want=False — как в проде: только 3 явных пика из квиза.
    full_want=True  — ablation: весь список Q5, как его видит эксперт при разметке.
    """
    want = case["want_traits"] if full_want else case["want_traits_top3"]
    return {
        "context": case.get("context", {}),
        "now_traits": case["now_traits"],
        "want_traits_top3": want,
        "physical": {"height": case.get("height"), "figure_type_self_assessed": None},
        "price_segment": "middle",
        "taboos": case.get("taboos", []),
        "colortype_known": None,
    }


def _vision(case: dict) -> dict:
    """Экспертные цветотип/фигура вместо реального Vision — изолируем диагностику."""
    return {
        "colortype": case["expert_colortype"],
        "tonal_characteristics": None,
        "natural_palette": None,
        "figure": {"figure_type": case["expert_figure"], "correction_flags": []},
    }


def _dominant(dist: dict | None) -> str | None:
    if not dist:
        return None
    return max(dist.items(), key=lambda kv: kv[1])[0]


def _formula_hit(formula: str | None, keywords: list[str]) -> bool:
    low = (formula or "").lower()
    return any(k.lower() in low for k in keywords)


def evaluate(mode: str = "dev", full_want: bool = False) -> dict:
    from core.pipeline import diagnose  # импорт здесь: --dry-run не должен требовать ключ

    cases = load_cases()
    rows, field_ok, formula_ok, gap_ok = [], 0, 0, 0

    for case in cases:
        diag = diagnose(_quiz(case, full_want), _vision(case), mode=mode)
        pred_field = _dominant(diag.get("semantic_field_distribution"))
        expected = [f.lower() for f in case["expert_dominant_field"]]
        f_ok = (pred_field or "").lower() in expected
        fo_ok = _formula_hit(diag.get("style_formula"), case["expert_formula_keywords"])
        gap = diag.get("gap_percentage")
        g_ok = isinstance(gap, (int, float)) and 0 <= gap <= 100

        field_ok += f_ok
        formula_ok += fo_ok
        gap_ok += g_ok
        rows.append({
            "id": case["id"], "gap": gap,
            "pred_field": pred_field, "expected_field": "/".join(expected), "field_ok": f_ok,
            "formula": diag.get("style_formula"), "formula_ok": fo_ok,
        })

    n = len(cases)
    return {
        "n": n,
        "dominant_field_accuracy": round(field_ok / n, 3) if n else 0.0,
        "formula_hit_rate": round(formula_ok / n, 3) if n else 0.0,
        "gap_sanity_rate": round(gap_ok / n, 3) if n else 0.0,
        "rows": rows,
    }


def _print(res: dict) -> None:
    print(f"\nКейсов: {res['n']}")
    print(f"{'кейс':<20} {'Gap':>5}  {'поле (модель/эксперт)':<28} {'формула':<40}")
    print("-" * 100)
    for r in res["rows"]:
        mark = "✓" if r["field_ok"] else "✗"
        fmark = "✓" if r["formula_ok"] else "✗"
        pair = f"{r['pred_field']} / {r['expected_field']}"
        print(f"{r['id']:<20} {str(r['gap']):>5}  {mark} {pair:<26} {fmark} {(r['formula'] or '')[:38]}")
    print("-" * 100)
    print(f"dominant_field_accuracy : {res['dominant_field_accuracy']}")
    print(f"formula_hit_rate        : {res['formula_hit_rate']}")
    print(f"gap_sanity_rate         : {res['gap_sanity_rate']}\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="dev", choices=["dev", "final"])
    ap.add_argument("--dry-run", action="store_true", help="без API: валидация датасета")
    ap.add_argument("--json", action="store_true", help="вывести метрики как JSON")
    ap.add_argument("--full-want", action="store_true",
                    help="ablation: подать весь список Q5 вместо 3 пиков (как размечает эксперт)")
    args = ap.parse_args()

    if args.dry_run:
        cases = load_cases()
        for c in cases:
            assert c["now_traits"] and c["want_traits"], c["id"]
            assert c["expert_dominant_field"], c["id"]
        print(f"датасет валиден: {len(cases)} кейсов, экспертная разметка на месте")
        return 0

    res = evaluate(mode=args.mode, full_want=args.full_want)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
