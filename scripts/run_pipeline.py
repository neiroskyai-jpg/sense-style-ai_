"""Сквозной пайплайн: фото + квиз → vision-анализ → диагностика Формулы стиля.

Пример:
    python -m scripts.run_pipeline портрет.jpg --height 168 --mode dev
По умолчанию берёт тестовый квиз tests/fixtures/sample_quiz.json.
"""
import argparse
import json
import sys
from pathlib import Path

from core.pipeline import analyze_photos, diagnose, generate_capsule

DEFAULT_QUIZ = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_quiz.json"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="фото + квиз → vision → диагностика")
    ap.add_argument("images", nargs="+", help="пути к фото (портрет, рост, профиль)")
    ap.add_argument("--quiz", default=str(DEFAULT_QUIZ), help="JSON с ответами квиза")
    ap.add_argument("--height", type=int, default=None, help="рост в см")
    ap.add_argument("--mode", default=None, choices=["dev", "final"])
    ap.add_argument("--capsule", action="store_true", help="добавить Шаг 3: капсулу образов")
    args = ap.parse_args()

    quiz = json.loads(Path(args.quiz).read_text(encoding="utf-8"))
    height = args.height or (quiz.get("physical") or {}).get("height")

    print("== Шаг 1. Vision-анализ ==", flush=True)
    vision = analyze_photos(args.images, height_cm=height, mode=args.mode)
    figure = vision.get("figure") or {}
    print(f"   цветотип: {vision.get('colortype')} ({vision.get('colortype_confidence')})")
    print(f"   фигура:   {figure.get('figure_type')} ({figure.get('figure_confidence')})")

    print("== Шаг 2. Диагностика Формулы стиля ==", flush=True)
    diag = diagnose(quiz, vision, mode=args.mode)
    print(f"   Identity Gap: {diag.get('gap_percentage')}%")
    print(f"   Формула стиля: {diag.get('style_formula')}")
    print(f"   распределение полей: {diag.get('semantic_field_distribution')}")

    if not args.capsule:
        print("\n(капсула пропущена — добавь --capsule для Шага 3)")
        return

    print("== Шаг 3. Капсула образов ==", flush=True)
    gen_req = {
        "mode": "capsule",
        "capsule_type": "auto",
        "season": "FW 2026-2027",
        "scenarios": ["работа", "деловые встречи", "повседневное", "выход"],
        "n_looks": 6,
        "price_segment": quiz.get("price_segment", "middle"),
        "taboos": quiz.get("taboos", []),
    }
    capsule = generate_capsule(diag, gen_req, mode=args.mode)
    cap = capsule.get("capsule") or {}
    items = cap.get("items") or []
    looks = capsule.get("looks") or []
    print(f"   вещей в капсуле: {len(items)} | комбинаций: {cap.get('combination_count')}")
    print(f"   образов: {len(looks)}")
    if looks:
        first = looks[0]
        print(f"\n   образ 1 — {first.get('scenario')}: {first.get('description')}")
        print(f"   image_generation_prompt:\n   {first.get('image_generation_prompt')}")


if __name__ == "__main__":
    main()
