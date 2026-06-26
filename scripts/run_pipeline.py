"""Сквозной пайплайн: фото + квиз → vision-анализ → диагностика Формулы стиля.

Пример:
    python -m scripts.run_pipeline портрет.jpg --height 168 --mode dev
По умолчанию берёт тестовый квиз tests/fixtures/sample_quiz.json.
"""
import argparse
import json
import sys
from pathlib import Path

from core.pipeline import analyze_photos, diagnose

DEFAULT_QUIZ = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_quiz.json"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="фото + квиз → vision → диагностика")
    ap.add_argument("images", nargs="+", help="пути к фото (портрет, рост, профиль)")
    ap.add_argument("--quiz", default=str(DEFAULT_QUIZ), help="JSON с ответами квиза")
    ap.add_argument("--height", type=int, default=None, help="рост в см")
    ap.add_argument("--mode", default=None, choices=["dev", "final"])
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
    print(f"   распределение полей: {diag.get('semantic_field_distribution')}")
    print("\n--- полный JSON диагностики (первые 1800 символов) ---")
    print(json.dumps(diag, ensure_ascii=False, indent=2)[:1800])


if __name__ == "__main__":
    main()
