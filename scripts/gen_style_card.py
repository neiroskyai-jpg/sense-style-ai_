"""Генерация карточки-эстетики базового стиля для выбора в анкете.

В отличие от обложек журнала — палитра БЕРЁТСЯ ИЗ САМОГО СТИЛЯ (Драма ≠ бежевая),
поэтому брендовую палитру НЕ навязываем. Анти-пластик-реализм (плёнка, текстура кожи) —
общий (см. память art-ai-nonplastic-photos).

Запуск:
    python -m scripts.gen_style_card <code> "<english prompt: образ этого стиля со своей палитрой>"
    code — classic | drama | romantic | natural (или подстиль)

Сохраняет web/photos/styles/<code>.png.
"""
import base64
import pathlib
import sys

from core import config, provider

DEST = pathlib.Path("web/photos/styles")
# pro-image надёжнее на реалистичных людях в полный рост (flash-image часто отказывает)
_MODEL = config.MODELS["image"]["dressing"]

_REALISM = (
    " Full-length fashion editorial photograph of one elegant natural woman aged 35 to 48, "
    "confident posture, refined styling, candid editorial framing, soft natural light. "
    "Shot on Kodak Portra 400 film, 85mm lens, authentic film grain, real unretouched skin with "
    "visible texture and pores, no beauty retouching, not plastic, not waxy, not CGI. "
    "No text, no words, no logos, no watermark. Vertical 4:5 composition."
)


def main():
    if len(sys.argv) < 3:
        print('Использование: python -m scripts.gen_style_card <code> "<english prompt>"')
        raise SystemExit(1)
    code, prompt = sys.argv[1], sys.argv[2]
    urls = provider.generate_image(prompt + _REALISM, model=_MODEL)
    data = urls[0].split(",", 1)[1]
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / (code + ".png")
    out.write_bytes(base64.b64decode(data))
    print("сохранено:", out, "| байт:", out.stat().st_size)


if __name__ == "__main__":
    main()
