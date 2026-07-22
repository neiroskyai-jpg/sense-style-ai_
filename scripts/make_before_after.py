"""Собрать коллаж «До → После» из двух фото под слайд презентации.

    python scripts/make_before_after.py <фото-до> <фото-после> <выход.png> [--vertical]

Зачем: слайд 2 широкий (10×5.62", ~1.78:1), а фото клиенток вертикальные. Одно фото на такой слайд
режется сильно; два рядом с подписями читаются как трансформация — то, ради чего слайд и нужен.
Коллаж собирается точно в пропорции слайда, чтобы put_photo_itmo не обрезал его повторно.

`--vertical` — кадры друг под другом, под узкий вертикальный плейсхолдер (3.24x5.62" на слайде
«Решение»). Горизонтальный коллаж в такой рамке обрезался бы почти до одного кадра.
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# Пропорция слайда 10×5.625". Коллаж делаем крупным — уменьшит уже PowerPoint.
OUT_W, OUT_H = 2136, 1200
# Вертикальный вариант в пропорции плейсхолдера 3.24x5.62"
OUT_W_V, OUT_H_V = 1000, 1735
GAP = 16              # зазор между кадрами
LABEL_H = 96          # полоса под подпись
BG = (245, 243, 240)  # тёплый светлый фон под палитру проекта
INK = (60, 30, 34)    # тёмно-бордовый текст


def fill_into(img: Image.Image, w: int, h: int, y_bias: float = 0.5) -> Image.Image:
    """Вписать по принципу «заполнить»: масштаб по короткой стороне, обрезка по длинной.

    y_bias — куда смещать обрезку по вертикали (0 — верх, 0.5 — центр). В ростовых портретах
    лицо в верхней трети кадра, и обрезка от центра срезала макушку: в вертикальном коллаже
    кадр сильно ниже исходника, лишнее уходит именно сверху.
    """
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    x = (resized.width - w) // 2
    y = round((resized.height - h) * min(max(y_bias, 0.0), 1.0))
    return resized.crop((x, y, x + w, y + h))


def load_font(size: int):
    for name in ("georgia.ttf", "Georgia.ttf", "times.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 1
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    vertical = "--vertical" in sys.argv
    before = Path(args[0])
    after = Path(args[1])
    out = Path(args[2])
    if not out.is_absolute():
        out = ROOT / out
    for p in (before, after):
        if not p.exists():
            print(f"Нет файла: {p}")
            return 1

    w, h = (OUT_W_V, OUT_H_V) if vertical else (OUT_W, OUT_H)
    canvas = Image.new("RGB", (w, h), BG)
    draw = ImageDraw.Draw(canvas)
    font = load_font(44 if vertical else 52)

    if vertical:   # кадры друг под другом, подпись под каждым
        cell_w, cell_h = w, (h - GAP - 2 * LABEL_H) // 2
        spots = [(Image.open(before), "До", 0),
                 (Image.open(after), "После", cell_h + LABEL_H + GAP)]
        for img, text, top in spots:
            canvas.paste(fill_into(img, cell_w, cell_h, y_bias=0.12), (0, top))
            box = draw.textbbox((0, 0), text, font=font)
            draw.text((w / 2 - (box[2] - box[0]) / 2,
                       top + cell_h + (LABEL_H - (box[3] - box[1])) / 2 - box[1]),
                      text, fill=INK, font=font)
    else:
        cell_w, cell_h = (w - GAP) // 2, h - LABEL_H
        canvas.paste(fill_into(Image.open(before), cell_w, cell_h), (0, 0))
        canvas.paste(fill_into(Image.open(after), cell_w, cell_h), (cell_w + GAP, 0))
        for text, cx in (("До", cell_w // 2), ("После", cell_w + GAP + cell_w // 2)):
            box = draw.textbbox((0, 0), text, font=font)
            draw.text((cx - (box[2] - box[0]) / 2,
                       cell_h + (LABEL_H - (box[3] - box[1])) / 2 - box[1]),
                      text, fill=INK, font=font)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out))
    target = 0.577 if vertical else 1.778
    print(f"OK: {out.relative_to(ROOT)} — {w}x{h} ({w / h:.3f}:1, рамка {target}:1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
