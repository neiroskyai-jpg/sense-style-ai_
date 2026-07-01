"""Генерация редакционного изображения для журнала (дух LICHI Inspire).

Современная fashion-editorial съёмка: элегантная модель, естественный свет,
люкс-минимализм, наша приглушённая палитра. Без текста и логотипов.

Запуск:
    python -m scripts.gen_editorial_image <dest_relpath> "<english prompt: сцена>" [orient]

    dest_relpath — путь внутри web/, напр. photos/blog/svoi-cveta.png
    orient       — portrait (4:5, по умолчанию) | landscape (16:9) | square (1:1)

Пример:
    python -m scripts.gen_editorial_image photos/blog/svoi-cveta-pochemu-odin-osvezhaet.png \
        "woman around 40 by a window holding two fabric swatches near her face, warm camel and cold grey"
"""
import base64
import pathlib
import sys

from core import provider

WEB = pathlib.Path("web")

_ORIENT = {
    "portrait": "Vertical 4:5 portrait composition.",
    "landscape": "Horizontal 16:9 composition.",
    "square": "Square 1:1 composition.",
}

# Дух LICHI Inspire: современная редакционная мода, живая элегантная модель,
# чистый свет, много воздуха, наша палитра. Тело-позитив: женщина 35–48.
_STYLE = (
    " Modern fashion editorial photograph, LICHI Inspire mood: one elegant woman aged 35 to 48, "
    "natural confident posture, refined minimal styling, real skin texture, candid editorial framing "
    "(often three-quarter or profile, face not centered), soft natural window light, generous negative space. "
    "Muted warm neutral palette — cream, camel, chocolate, taupe, soft grey — with a single deep wine accent. "
    "Photorealistic, high-end magazine quality, film-like grain. "
    "No text, no words, no logos, no captions, no watermark, no brand names."
)


def main():
    if len(sys.argv) < 3:
        print('Использование: python -m scripts.gen_editorial_image <dest_relpath> "<english prompt>" [orient]')
        raise SystemExit(1)
    dest_rel = sys.argv[1]
    prompt = sys.argv[2]
    orient = sys.argv[3] if len(sys.argv) > 3 else "portrait"
    tail = _ORIENT.get(orient, _ORIENT["portrait"])

    urls = provider.generate_image(prompt + _STYLE + " " + tail)
    data = urls[0].split(",", 1)[1]
    out = WEB / dest_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(data))
    print("сохранено:", out, "| байт:", out.stat().st_size)


if __name__ == "__main__":
    main()
