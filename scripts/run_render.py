"""Identity-preserving рендер: фото клиентки → одна база личности → образы на ней.

Двухступенчатый конвейер (GPT держит лицо/фигуру → Gemini одевает). База считается
один раз и переиспользуется на все образы (консистентность + экономия).

Пример:
    python -m scripts.run_render фото.jpg --look "graphite blazer, milk peplum blouse, ruby pumps" \
        --look "graphite knit dress with belted waist, ruby loafers" -o ab-output/render
"""
import argparse
import sys

from core.pipeline import render_capsule_on_client
from core.provider import save_data_url


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="фото клиентки → образы на ней (один человек)")
    ap.add_argument("photo", help="фото клиентки (референс личности)")
    ap.add_argument("--look", action="append", required=True, dest="looks",
                    help="промпт образа (можно несколько раз) = looks[].image_generation_prompt")
    ap.add_argument("-o", "--out", default="ab-output/render", help="префикс выходных файлов")
    args = ap.parse_args()

    looks = render_capsule_on_client(args.photo, args.looks)
    for i, url in enumerate(looks, 1):
        path = save_data_url(url, f"{args.out}-look{i}.png")
        print(f"образ {i}: {path}")


if __name__ == "__main__":
    main()
