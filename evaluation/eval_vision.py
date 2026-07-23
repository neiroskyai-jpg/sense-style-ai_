"""Eval Vision: цветотип по фото против экспертной разметки (DS-критерий конкурса).

Дополняет `eval_diagnosis`, где Vision намеренно изолирован: там из разметки берутся цветотип и
фигура, чтобы мерить диагностику отдельно от чтения фото. Здесь наоборот — мерим само чтение.

Разметка: имя папки в `data/colortype-calibration/` и есть экспертная метка (`autumn_natural`,
`summer_natural`, `winter_natural`). Размечал автор метода. Формат совпадает с ключом выхода
Vision (`<сезон>_<подтип>`), поэтому сравнение прямое, без маппинга.

Что измеряем:
  1. exact_accuracy   — полное совпадение цветотипа из 12. Главная метрика.
  2. season_accuracy  — угадан сезон (весна/лето/осень/зима) при возможной ошибке в подтипе.
     Разделяем сознательно: ошибка в сезоне уводит всю палитру, ошибка в подтипе смещает
     контраст — цена у них разная, и лечатся они разными правилами промпта.
  3. top2_accuracy    — попадание с учётом `colortype_alternative` (второй кандидат модели).
  4. confidence_calibration — совпадает ли уверенность модели с тем, права ли она. Модель,
     уверенно ошибающаяся, опаснее сомневающейся: на её ответе строится палитра клиентки.

ВЫБОРКА МАЛА: 9 фото, 3 класса из 12. Это proof of method, не статистика — как и n=3 в
eval_diagnosis. Числа показывают, что контур оценки работает и что мерить; расширение —
вопрос накопления размеченных фото, а не переписывания скрипта.

Запуск (нужен OPENROUTER_API_KEY; ~1-3 цента на фото):
    python -m evaluation.eval_vision
    python -m evaluation.eval_vision --dry-run     # без API: проверка датасета
    python -m evaluation.eval_vision --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

_DATASET = Path(__file__).resolve().parent.parent / "data" / "colortype-calibration"
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def load_cases() -> list[dict]:
    """Фото из размеченных папок. Имя папки — метка эксперта."""
    cases = []
    for folder in sorted(p for p in _DATASET.iterdir() if p.is_dir()):
        for photo in sorted(folder.iterdir()):
            if photo.suffix.lower() in _IMAGE_SUFFIXES:
                cases.append({"photo": photo, "expert_colortype": folder.name})
    return cases


def _season(colortype: str | None) -> str | None:
    return colortype.split("_")[0] if colortype else None


def evaluate(cases: list[dict], mode: str | None = None) -> dict:
    from core.pipeline import analyze_photos

    rows = []
    for case in cases:
        expert = case["expert_colortype"]
        try:
            vision = analyze_photos([str(case["photo"])], mode=mode)
        except Exception as exc:  # noqa: BLE001 — одно упавшее фото не должно ронять прогон
            rows.append({"photo": case["photo"].name, "expert": expert, "error": str(exc)[:200]})
            continue
        predicted = vision.get("colortype")
        alternative = vision.get("colortype_alternative")
        rows.append({
            "photo": case["photo"].name,
            "expert": expert,
            "predicted": predicted,
            "alternative": alternative,
            "confidence": vision.get("colortype_confidence"),
            "exact": predicted == expert,
            "season": _season(predicted) == _season(expert),
            "top2": expert in {predicted, alternative},
        })
    return {"rows": rows, "metrics": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if "error" not in r]
    n = len(scored)
    if not n:
        return {"n": 0, "errors": len(rows)}

    def share(key: str) -> float:
        return round(sum(1 for r in scored if r[key]) / n, 3)

    # Уверенность против правоты: high при ошибке — худший случай, его выносим отдельно.
    confident_wrong = sum(1 for r in scored if r["confidence"] == "high" and not r["exact"])
    return {
        "n": n,
        "errors": len(rows) - n,
        "exact_accuracy": share("exact"),
        "season_accuracy": share("season"),
        "top2_accuracy": share("top2"),
        "confident_but_wrong": confident_wrong,
        "confusion": dict(Counter(f'{r["expert"]} → {r["predicted"]}' for r in scored if not r["exact"])),
    }


def _report(result: dict) -> None:
    m = result["metrics"]
    print(f'\nVision-eval · n={m["n"]}' + (f' · сбоев: {m["errors"]}' if m.get("errors") else ""))
    if not m["n"]:
        # Раньше отчёт падал по KeyError и прятал настоящую причину — а она одна на все фото
        # и куда важнее метрик: кончился ключ, недоступна сеть, сменился формат ответа.
        print("  Ни одно фото не прошло. Причина первого сбоя:")
        first = next((r for r in result["rows"] if "error" in r), None)
        print(f'    {first["error"]}' if first else "    (нет данных)")
        return
    print(f'  exact_accuracy   {m["exact_accuracy"]}   полное совпадение цветотипа из 12')
    print(f'  season_accuracy  {m["season_accuracy"]}   угадан сезон (ошибка только в подтипе)')
    print(f'  top2_accuracy    {m["top2_accuracy"]}   с учётом второго кандидата модели')
    if m["confident_but_wrong"]:
        print(f'  [!] уверенно ошиблась на {m["confident_but_wrong"]} фото — самый дорогой случай')
    if m["confusion"]:
        print("\n  Куда путает:")
        for pair, count in sorted(m["confusion"].items(), key=lambda kv: -kv[1]):
            print(f"    {pair.replace(chr(0x2192), '->')} x{count}")
    print("\n  Выборка мала (9 фото, 3 класса из 12) — это proof of method, не статистика.")


def main() -> int:
    try:  # консоль Windows по умолчанию cp1251 — не печатает часть символов
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default=None, help="dev | final — тир модели из core.config")
    ap.add_argument("--dry-run", action="store_true", help="без API: только проверить датасет")
    ap.add_argument("--json", dest="json_out", help="куда сложить сырой результат")
    args = ap.parse_args()

    cases = load_cases()
    if not cases:
        print(f"Нет размеченных фото в {_DATASET}")
        return 1

    by_label = Counter(c["expert_colortype"] for c in cases)
    print(f"Датасет: {len(cases)} фото, {len(by_label)} классов")
    for label, count in sorted(by_label.items()):
        print(f"  {label:20s} {count}")

    if args.dry_run:
        print("\n--dry-run: API не вызывался.")
        return 0

    result = evaluate(cases, mode=args.mode)
    # JSON пишем ДО печати: вывод в консоль Windows (cp1251) может упасть на не-ASCII, а платный
    # результат прогона терять нельзя.
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                                  default=str), encoding="utf-8")
    _report(result)
    if args.json_out:
        print(f"\nСырой результат: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
