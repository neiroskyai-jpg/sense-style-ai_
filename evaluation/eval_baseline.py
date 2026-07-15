"""Baseline vs Sense Style — доказательство ценности пайплайна (DS-критерий конкурса).

Вопрос жюри: «чем вы лучше обычного промпта к LLM?» Здесь честный ответ цифрами.

Сравниваем на ОДНОМ eval-наборе (evaluation/ground_truth.json, экспертная разметка) два подхода:

  BASELINE   — «просто спросить LLM»: минимальный промпт, ответы квиза → стиль + распределение
               по 4 полям. Без методологии, без RAG, без структурной диагностики.
  SENSE STYLE — наш пайплайн: формула-диагностик промпт (авторская таксономия 4 поля / 25 подстилей,
               field-aware Style Gap) + RAG по базе знаний.

Метрики одинаковы для обоих (из eval_diagnosis): попадание в доминантное поле эксперта и в ключевые
слова Формулы. Так видно вклад именно методологии, а не модели (модель одна и та же).

Запуск (нужен OPENROUTER_API_KEY; модель flash, ~1-2 цента на кейс):
    python -m evaluation.eval_baseline                # dev-модель
    python -m evaluation.eval_baseline --mode final   # «думающая»
    python -m evaluation.eval_baseline --md           # готовая markdown-таблица для подачи
"""
from __future__ import annotations

import argparse
import json

from evaluation.eval_diagnosis import _dominant, _formula_hit, _quiz, _vision, load_cases

# Минимальный, «наивный» промпт — фейр-baseline «просто спросил нейросеть». Даём только 4 категории,
# чтобы выход был сопоставим по метрике, но НЕ даём методологию, шаги, подстили, правила, RAG.
_BASELINE_SYSTEM = (
    "Ты — стилист. По ответам человека определи его стиль. "
    "Оцени, какое впечатление он ХОЧЕТ транслировать, распределив 100% между четырьмя полями: "
    "natural (натуральный, естественный), romance (романтичный, мягкий), drama (яркий, властный, "
    "эффектный), classic (классический, деловой, минимализм). "
    "Верни СТРОГО JSON: {\"style_formula\": \"<короткая формула стиля>\", "
    "\"semantic_field_distribution\": {\"natural\": <int>, \"romance\": <int>, \"drama\": <int>, "
    "\"classic\": <int>}} — сумма ровно 100."
)


def baseline_diagnose(quiz: dict, vision: dict, mode: str = "dev") -> dict:
    """«Просто спросить LLM» — та же модель, что в пайплайне, но без методологии и RAG."""
    from core import config, provider
    payload = {**quiz, "colortype": vision.get("colortype"),
               "figure_type": (vision.get("figure") or {}).get("figure_type")}
    return provider.chat_json(config.model_for("text", mode), _BASELINE_SYSTEM,
                              json.dumps(payload, ensure_ascii=False), max_tokens=1024)


def _metrics(diag_fn, cases: list[dict], mode: str, full_want: bool = False) -> dict:
    field_ok, formula_ok, rows = 0, 0, []
    for case in cases:
        diag = diag_fn(_quiz(case, full_want), _vision(case), mode)
        pred = _dominant(diag.get("semantic_field_distribution"))
        expected = [f.lower() for f in case["expert_dominant_field"]]
        f_ok = (pred or "").lower() in expected
        fo_ok = _formula_hit(diag.get("style_formula"), case["expert_formula_keywords"])
        field_ok += f_ok
        formula_ok += fo_ok
        rows.append({"id": case["id"], "pred": pred, "expected": "/".join(expected),
                     "field_ok": f_ok, "formula": diag.get("style_formula"), "formula_ok": fo_ok})
    n = len(cases)
    return {"n": n, "dominant_field_accuracy": round(field_ok / n, 3) if n else 0.0,
            "formula_hit_rate": round(formula_ok / n, 3) if n else 0.0, "rows": rows}


def compare(mode: str = "dev") -> dict:
    from core.pipeline import diagnose

    cases = load_cases()
    base = _metrics(lambda q, v, m: baseline_diagnose(q, v, m), cases, mode)
    ours = _metrics(lambda q, v, m: diagnose(q, v, mode=m), cases, mode)
    return {"n": len(cases), "baseline": base, "sense_style": ours}


def _print(res: dict) -> None:
    b, s = res["baseline"], res["sense_style"]
    print(f"\nКейсов: {res['n']} (n мал — оговорка честная; расширять на прогоне клиенток)\n")
    print(f"{'метод':<16} {'field_accuracy':>15} {'formula_hit':>13}")
    print("-" * 46)
    print(f"{'Baseline LLM':<16} {b['dominant_field_accuracy']:>15} {b['formula_hit_rate']:>13}")
    print(f"{'Sense Style':<16} {s['dominant_field_accuracy']:>15} {s['formula_hit_rate']:>13}")
    print("-" * 46)
    print("\nПо кейсам (поле: модель / эксперт):")
    for rb, rs in zip(b["rows"], s["rows"]):
        print(f"  {rb['id']:<12} baseline {('✓' if rb['field_ok'] else '✗')} {rb['pred'] or '—':<10} "
              f"| sense {('✓' if rs['field_ok'] else '✗')} {rs['pred'] or '—':<10} (эксперт: {rs['expected']})")


def _md(res: dict) -> str:
    b, s = res["baseline"], res["sense_style"]
    return (
        f"### Baseline vs Sense Style (n={res['n']})\n\n"
        "| Метод | Совпадение с доминантным полем эксперта | Попадание в ключевые слова Формулы |\n"
        "|---|---|---|\n"
        f"| Baseline LLM (просто промпт) | {b['dominant_field_accuracy']} | {b['formula_hit_rate']} |\n"
        f"| **Sense Style (пайплайн + RAG)** | **{s['dominant_field_accuracy']}** | **{s['formula_hit_rate']}** |\n\n"
        f"> Одна и та же модель; отличие — авторская методология (4 поля / 25 подстилей, field-aware "
        f"Style Gap) и RAG. n={res['n']} — статистически слабо, расширяется на прогоне клиенток.\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="dev", choices=["dev", "final"])
    ap.add_argument("--md", action="store_true", help="вывести markdown-таблицу для подачи")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = compare(mode=args.mode)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif args.md:
        print(_md(res))
    else:
        _print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
