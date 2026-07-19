"""Библиотека предметных фото для Карты: по одному кадру на тип вещи.

Зачем именно библиотека, а не генерация под клиентку. Картинка вещи в Карте — иллюстрация типа
(«вот такой жакет»), а не снимок конкретного товара: каталог брендов мы пока не подключаем.
Иллюстрации повторяются от клиентки к клиентке, поэтому платить за них каждый раз незачем.
Генерируем один раз, кладём в репозиторий — дальше показ бесплатный и мгновенный.

Запуск (генерирует только недостающее, уже готовое не перезаписывает):
    python -m scripts.gen_item_images            # всё недостающее
    python -m scripts.gen_item_images жакет юбка # только указанные
    python -m scripts.gen_item_images --limit 2  # первые N недостающих (проба на качество)

Файлы: web/photos/items/<slug>.jpg
"""
from __future__ import annotations

import pathlib
import sys

from PIL import Image

from core import provider

DEST = pathlib.Path("web/photos/items")

# Предметная съёмка, а не съёмка на модели: в капсуле нужна сама вещь. Фон — наш кремовый,
# чтобы карточка не выглядела вырезкой из чужого магазина. Палитра нейтральная: кадр служит
# примером типа вещи и не должен спорить с личной палитрой клиентки.
_STYLE = (
    " Product packshot photograph of a single WOMEN'S garment on a plain seamless background "
    "in warm cream tone (#F5EFE3), soft even studio light, gentle natural shadow beneath, "
    "garment neatly presented and fully visible within the frame, generous margin around it. "
    "Womenswear: feminine cut and proportions, narrower shoulders, softer construction — "
    "clearly a woman's piece, not menswear. "
    "Muted warm neutral palette. Quiet-luxury, understated, high quality fabric with visible "
    "natural texture and honest drape. "
    "Shot on medium-format camera, true-to-life colors, natural soft contrast, subtle film grain. "
    "No person, no model, no mannequin, no hands, no face, no body parts. "
    "Not a 3D render, not CGI, not illustration, not flat lay collage. "
    "No text, no words, no logos, no labels, no price tags, no watermark, no brand names. "
    "Square 1:1 composition, the garment centered."
)

# Типы вещей и англоязычное описание кадра. Цвет — нейтральный: кадр иллюстрирует ТИП,
# а личная палитра клиентки живёт в тексте карточки и в блоке палитры.
ITEMS: dict[str, str] = {
    "жакет": "a tailored semi-fitted blazer in warm taupe wool",
    "пальто": "a straight midi wool coat in camel",
    "плащ": "a midi leather trench coat in soft cocoa brown",
    "тренч": "a classic belted trench coat in sand beige cotton gabardine",
    "блуза": "a silk blouse in soft ivory with fluid drape",
    "рубашка": "a crisp cotton shirt in off-white",
    "джемпер": "a fine-knit cashmere jumper in oatmeal beige",
    "водолазка": "a fine-knit turtleneck in warm grey",
    "футболка": "a heavy cotton t-shirt in cream with round neckline",
    "платье": "a midi wrap dress in deep burgundy fluid fabric",
    "юбка": "a midi skirt in warm chocolate satin, softly draped",
    "брюки": "straight-leg tailored trousers in stone grey wool",
    "джинсы": "straight-leg jeans in mid indigo denim",
    "туфли": "a pair of leather slingback pumps in dusty rose",
    "ботильоны": "a pair of leather ankle boots in milky cream on a stable block heel",
    "лоферы": "a pair of leather loafers in cognac brown",
    "сапоги": "a pair of tall leather boots in dark chocolate",
    "сумка": "a structured leather tote bag in deep wine",
    "ремень": "a slim leather belt in tan with an understated buckle",
    "шарф": "a soft wool scarf in muted camel, loosely folded",
}


# Модель отдаёт PNG 1024x1024 весом ~1.5 МБ. В карточке кадр показывается в ~130 px, поэтому
# двадцать таких файлов — это 29 МБ в репозитории и заметная задержка на мобильном интернете.
# Ужимаем до 640 px и переводим в JPEG: вес падает примерно в тридцать раз, разница не видна.
_MAX_SIDE = 640
_QUALITY = 82


def slug(name: str) -> str:
    return name.strip().lower()


def optimize(src: pathlib.Path, dest: pathlib.Path) -> None:
    """PNG от модели → компактный JPEG. Исходник удаляем: в репозитории он не нужен."""
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)
        im.save(dest, "JPEG", quality=_QUALITY, optimize=True, progressive=True)
    if src != dest:
        src.unlink(missing_ok=True)


def missing() -> list[str]:
    return [k for k in ITEMS if not (DEST / f"{slug(k)}.jpg").exists()]


def main() -> None:
    args = [a for a in sys.argv[1:]]
    limit = None
    if "--limit" in args:
        i = args.index("--limit")
        limit = int(args[i + 1])
        del args[i:i + 2]

    todo = [a for a in args if a in ITEMS] if args else missing()
    if limit:
        todo = todo[:limit]
    if not todo:
        print("Всё уже сгенерировано, платить не за что.")
        return

    DEST.mkdir(parents=True, exist_ok=True)
    print(f"К генерации: {len(todo)} — {', '.join(todo)}")
    ok, failed = 0, []
    for name in todo:
        raw = DEST / f"{slug(name)}.raw.png"
        dest = DEST / f"{slug(name)}.jpg"
        try:
            urls = provider.generate_image(ITEMS[name] + _STYLE)
            provider.save_data_url(urls[0], raw)
            optimize(raw, dest)
            ok += 1
            print(f"  [ok] {name} -> {dest}")
        except Exception as e:  # noqa: BLE001 — одна неудача не должна ронять весь прогон
            failed.append(name)
            print(f"  [!!] {name}: {str(e)[:200]}")
    print(f"Готово: {ok} из {len(todo)}." + (f" Не вышло: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
