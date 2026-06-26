"""CLI для первого живого вызова Vision.

Пример:
    python -m scripts.run_vision портрет.jpg рост.jpg --height 165 --mode dev
"""
import argparse
import json
import sys

from core.pipeline import analyze_photos


def main() -> None:
    # Windows-консоль по умолчанию не UTF-8 → кириллица в кракозябрах. Чиним вывод.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Vision-анализ фото клиентки → JSON")
    ap.add_argument("images", nargs="+", help="пути к фото (портрет, рост, профиль)")
    ap.add_argument("--height", type=int, default=None, help="рост в см")
    ap.add_argument("--mode", default=None, choices=["dev", "final"], help="тир моделей")
    args = ap.parse_args()

    result = analyze_photos(args.images, height_cm=args.height, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
