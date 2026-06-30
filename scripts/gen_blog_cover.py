"""Генерация обложки статьи блога (editorial, без людей и текста).

Запуск:
    python -m scripts.gen_blog_cover <slug> "<english image prompt: что на обложке>"

Сохраняет web/photos/blog/<slug>.png. Затем в frontmatter статьи добавь:
    cover: /photos/blog/<slug>.png
"""
import base64
import pathlib
import sys

from core import provider

DEST = pathlib.Path("web/photos/blog")

_STYLE = (" Editorial fashion still life, no people, no faces, no text, no logos, no words. "
          "Muted warm neutral palette (cream, camel, chocolate, soft grey) with a deep wine accent, "
          "soft natural window light, refined minimal Petrogradka editorial mood, photorealistic. "
          "Horizontal 16:9 composition.")


def main():
    if len(sys.argv) < 3:
        print('Использование: python -m scripts.gen_blog_cover <slug> "<english prompt>"')
        raise SystemExit(1)
    slug, prompt = sys.argv[1], sys.argv[2]
    urls = provider.generate_image(prompt + _STYLE)
    data = urls[0].split(",", 1)[1]
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / (slug + ".png")
    out.write_bytes(base64.b64decode(data))
    print("сохранено:", out, "| байт:", out.stat().st_size)


if __name__ == "__main__":
    main()
