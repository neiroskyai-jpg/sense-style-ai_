"""Загрузка партнёрского XML-фида → нормализация → data/catalog/<name>.json.

Использование (когда придёт ссылка на фид Lamoda):
    python scripts/load_catalog.py <URL-или-путь-к-XML> [имя]

Пример:
    python scripts/load_catalog.py "https://cpa-lamoda.ru/feed/xml?id=1863" lamoda

Дальше JSON подключается к подбору (core.catalog.match_products) и к результату квиза.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.catalog import parse_feed  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "catalog"


def _load_xml(src: str) -> str:
    if src.startswith("http://") or src.startswith("https://"):
        req = urllib.request.Request(src, headers={"User-Agent": "SenseStyle/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return r.read().decode("utf-8", errors="replace")
    return Path(src).read_text(encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "catalog"

    xml_text = _load_xml(src)
    products = parse_feed(xml_text)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{name}.json"
    out.write_text(
        json.dumps([p.as_dict() for p in products], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    in_stock = sum(1 for p in products if p.in_stock)
    with_img = sum(1 for p in products if p.image)
    print(f"Распознано вещей: {len(products)} (в наличии: {in_stock}, с фото: {with_img})")
    print(f"Сохранено: {out}")
    if products:
        print("\nПример первой вещи:")
        print(json.dumps(products[0].as_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
