"""Генерация образа по текстовому промпту через Seedream (OpenRouter).

Пример:
    python -m scripts.run_image "A woman in a graphite blazer..." -o ab-output/look1.png
"""
import argparse
import sys

from core.provider import generate_image, save_data_url


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="промпт → изображение (Seedream/Nano Banana)")
    ap.add_argument("prompt", help="image_generation_prompt из капсулы")
    ap.add_argument("-o", "--out", default="ab-output/look.png", help="куда сохранить")
    ap.add_argument("--model", default=None, help="слаг модели OpenRouter (по умолчанию Seedream)")
    ap.add_argument("--ref", nargs="*", default=None, help="референс-изображения (мульти-референс)")
    args = ap.parse_args()

    urls = generate_image(args.prompt, model=args.model, ref_images=args.ref)
    for i, url in enumerate(urls):
        out = args.out if len(urls) == 1 else args.out.replace(".png", f"-{i}.png")
        path = save_data_url(url, out)
        print(f"сохранено: {path}")


if __name__ == "__main__":
    main()
