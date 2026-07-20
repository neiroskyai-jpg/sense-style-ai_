"""Собрать коллаж «До → После» из двух фото под слайд презентации.

    python scripts/make_before_after.py <фото-до> <фото-после> <выход.png>

Зачем: слайд 2 широкий (10×5.62", ~1.78:1), а фото клиенток вертикальные. Одно фото на такой слайд
режется сильно; два рядом с подписями читаются как трансформация — то, ради чего слайд и нужен.
Коллаж собирается точно в пропорции слайда, чтобы put_photo_itmo не обрезал его повторно.
"""
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent

# Пропорция слайда 10×5.625". Коллаж делаем крупным — уменьшит уже PowerPoint.
OUT_W, OUT_H = 2136, 1200
GAP = 16              # зазор между кадрами
LABEL_H = 96          # полоса под подпись
BG = (245, 243, 240)  # тёплый светлый фон под палитру проекта
INK = (60, 30, 34)    # тёмно-бордовый текст


def fill_into(img: Image.Image, w: int, h: int) -> Image.Image:
    """Вписать по принципу «заполнить»: масштаб по короткой стороне, обрезка по длинной, центр."""
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    resized = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    x = (resized.width - w) // 2
    y = (resized.height - h) // 2
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
    before = Path(sys.argv[1])
    after = Path(sys.argv[2])
    out = Path(sys.argv[3])
    if not out.is_absolute():
        out = ROOT / out
    for p in (before, after):
        if not p.exists():
            print(f"Нет файла: {p}")
            return 1

    cell_w = (OUT_W - GAP) // 2
    cell_h = OUT_H - LABEL_H

    canvas = Image.new("RGB", (OUT_W, OUT_H), BG)
    canvas.paste(fill_into(Image.open(before), cell_w, cell_h), (0, 0))
    canvas.paste(fill_into(Image.open(after), cell_w, cell_h), (cell_w + GAP, 0))

    draw = ImageDraw.Draw(canvas)
    font = load_font(52)
    for text, cx in (("До", cell_w // 2), ("После", cell_w + GAP + cell_w // 2)):
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (box[2] - box[0]) / 2, cell_h + (LABEL_H - (box[3] - box[1])) / 2 - box[1]),
                  text, fill=INK, font=font)

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out))
    print(f"OK: {out.relative_to(ROOT)} — {OUT_W}x{OUT_H} ({OUT_W / OUT_H:.3f}:1, слайд 1.778:1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
