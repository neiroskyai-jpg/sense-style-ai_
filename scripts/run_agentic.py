"""Демо агентного self-correction: рендер образа с проверкой судьёй и авто-перегенерацией.

Пример:
    python -m scripts.run_agentic фото.jpg --look "graphite blazer, milk peplum blouse, ruby pumps"
"""
import argparse
import json
import sys

from core.provider import save_data_url
from evaluation.self_correct import render_look_validated
from scripts.run_eval import DEMO_DIAGNOSIS


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="self-correcting рендер (генерация + судья + ретрай)")
    ap.add_argument("photo")
    ap.add_argument("--look", required=True, help="image_generation_prompt образа")
    ap.add_argument("--threshold", type=float, default=0.7)
    ap.add_argument("--attempts", type=int, default=2)
    ap.add_argument("-o", "--out", default="ab-output/agentic.png")
    args = ap.parse_args()

    res = render_look_validated(args.photo, args.look, DEMO_DIAGNOSIS,
                               threshold=args.threshold, max_attempts=args.attempts)
    path = save_data_url(res["img"], args.out)
    print(f"попыток: {res['attempt']} | принято: {res['accepted']}")
    print("метрики:", json.dumps(res["scores"], ensure_ascii=False))
    print("сохранено:", path)


if __name__ == "__main__":
    main()
