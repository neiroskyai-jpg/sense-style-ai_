"""Прогон LLM-судьи: образ(ы) + референс + диагностика → метрики качества.

Пример:
    python -m scripts.run_eval ab-output/render-look2.png --ref фото.jpg
"""
import argparse
import json
import sys
from pathlib import Path

from evaluation.judge import judge_look

# демо-диагностика кейса 01-do (winter_natural, прямоугольник, графит/молочный/рубин)
DEMO_DIAGNOSIS = {
    "style_formula": "Минимализм × Power Woman × Классика-доминанта",
    "figure_type": "rectangle",
    "colortype": "winter_natural",
    "tonal_characteristics": {"undertone": "cool", "depth": "medium", "contrast": "high"},
    "visual_formula": {
        "palette": [
            {"name": "графит", "role": "base"},
            {"name": "молочный", "role": "base"},
            {"name": "рубин", "role": "accent"},
        ],
        "stop_list": ["платье-футляр", "мешковатые силуэты"],
    },
}


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="LLM-судья: образ vs диагностика")
    ap.add_argument("looks", nargs="+", help="пути к сгенерированным образам")
    ap.add_argument("--ref", default=None, help="фото клиентки (для сходства)")
    ap.add_argument("--diagnosis", default=None, help="JSON-файл диагностики (иначе демо)")
    args = ap.parse_args()

    diag = (json.loads(Path(args.diagnosis).read_text(encoding="utf-8"))
            if args.diagnosis else DEMO_DIAGNOSIS)

    for look in args.looks:
        scores = judge_look(look, diag, reference_photo=args.ref)
        print(f"\n=== {look} ===")
        print(json.dumps(scores, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
