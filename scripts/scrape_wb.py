"""Скрейпер Wildberries под packshot-каталог для раскладки (капсула-коллаж).

Тянет товары локальных брендов quiet-luxury (наводка фаундера) через публичный
search-API WB и строит CSV в формате core/catalog.py (parse_csv).

ПОЧЕМУ WB: у карточек WB есть предметные фото (packshot) И покупаемые ссылки
(+ партнёрка WB), чего не было у Lichi/Ushatava (фото на моделях, узкие стили).

ЗАПУСК (сеть нужна — гоняет фаундер):
    python scripts/scrape_wb.py                      # все бренды, ~40 товаров каждый
    python scripts/scrape_wb.py --brand Lime --limit 60
    python scripts/scrape_wb.py --out data/fashion-base/products_wb.csv

ХРУПКОСТЬ WB (проверить на первом прогоне):
- search-API может требовать актуальный dest/appType — если пусто, см. RAW-дамп (--debug).
- basket-хост картинки строится по формуле от nm; для новых (высоких) nm таблица хостов
  расширяется — если картинка 404, добить _basket_host() новыми диапазонами.
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "fashion-base" / "products_wb.csv"

# Бренды quiet-luxury (память wb-brands-quiet-luxury). style — стилевой тег метода для скоринга.
BRANDS = [
    {"query": "Lime",          "brand": "Lime",          "style": "classic"},
    {"query": "Mollis",        "brand": "Mollis",        "style": "classic"},
    {"query": "Studio 29",     "brand": "Studio 29",     "style": "classic natural"},
    {"query": "To Be Blossom", "brand": "To Be Blossom", "style": "natural romance"},
    {"query": "Zarina Premium","brand": "Zarina",        "style": "classic"},
]

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "application/json",
}
# search-API WB (v5). dest — регион (Москва); curr — валюта.
_SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"
_SEARCH_PARAMS = {
    "appType": "1", "curr": "rub", "dest": "-1257786",
    "resultset": "catalog", "sort": "popular", "spp": "30",
    "suppressSpellcheck": "false",
}

# категории метода из ключевых слов названия WB (нужно для match_products по слотам)
_CAT_KW = [
    ("пиджак",   ["жакет", "блейзер", "пиджак", "блэйзер"]),
    ("пальто",   ["пальто", "тренч", "шуба", "дублёнка", "дубленка"]),
    ("платье",   ["платье", "сарафан"]),
    ("юбка",     ["юбка"]),
    ("брюки",    ["брюки", "палаццо", "джинсы", "штаны", "легинсы"]),
    ("трикотаж", ["свитер", "джемпер", "кардиган", "водолазка", "пуловер", "трикотаж", "лонгслив"]),
    ("рубашка",  ["рубашка", "блуза", "сорочка"]),
    ("топ",      ["топ", "футболка", "майка", "боди"]),
    ("аксессуар",["сумка", "ремень", "платок", "шарф", "очки", "перчатки"]),
]


def _category(name: str) -> str:
    low = name.lower()
    for cat, kws in _CAT_KW:
        if any(k in low for k in kws):
            return cat
    return "одежда"


def _basket_host(nm: int) -> str:
    """basket-хост картинки WB по номенклатуре. Таблица периодически расширяется —
    при 404 на новых nm добавить диапазон (последний else — самый свежий)."""
    vol = nm // 100000
    ranges = [(143,"01"),(287,"02"),(431,"03"),(719,"04"),(1007,"05"),(1061,"06"),
              (1115,"07"),(1169,"08"),(1313,"09"),(1601,"10"),(1655,"11"),(1919,"12"),
              (2045,"13"),(2189,"14"),(2405,"15"),(2621,"16"),(2837,"17"),(3053,"18"),
              (3269,"19"),(3485,"20"),(3701,"21"),(3917,"22"),(4133,"23"),(4349,"24"),
              (4565,"25")]
    for hi, host in ranges:
        if vol <= hi:
            return host
    return "26"  # свежие товары — самый новый basket; проверить вживую


def _image_url(nm: int) -> str:
    vol = nm // 100000
    part = nm // 1000
    host = _basket_host(nm)
    return f"https://basket-{host}.wbbasket.ru/vol{vol}/part{part}/{nm}/images/big/1.webp"


def _price(p: dict) -> float:
    # WB отдаёт цену в копейках; в разных версиях — salePriceU или sizes[].price.product
    for key in ("salePriceU", "priceU"):
        if p.get(key):
            return round(p[key] / 100)
    for s in (p.get("sizes") or []):
        pr = (s.get("price") or {}).get("product")
        if pr:
            return round(pr / 100)
    return 0.0


def _color(p: dict) -> str:
    cols = p.get("colors") or []
    if cols and isinstance(cols[0], dict):
        return cols[0].get("name", "")
    return ""


def fetch_brand(cfg: dict, limit: int, debug: bool = False) -> list[dict]:
    params = {**_SEARCH_PARAMS, "query": cfg["query"]}
    r = requests.get(_SEARCH_URL, params=params, headers=_HEADERS, timeout=30)
    if r.status_code >= 400:
        print(f"  [{cfg['brand']}] HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    data = r.json()
    products = (data.get("data") or {}).get("products") or []
    if debug:
        print(f"  RAW keys: {list(data.keys())}; products: {len(products)}", file=sys.stderr)
        if products:
            print(f"  sample product keys: {list(products[0].keys())}", file=sys.stderr)
    rows = []
    want = cfg["brand"].lower()
    for p in products[: limit * 3]:  # берём с запасом — потом фильтр по бренду
        brand = (p.get("brand") or "").strip()
        if want not in brand.lower():
            continue  # WB подмешивает смежные бренды — оставляем только целевой
        nm = p.get("id")
        if not nm:
            continue
        name = (p.get("name") or "").strip()
        rows.append({
            "id": str(nm), "name": name, "brand": cfg["brand"],
            "category": _category(name), "price": _price(p), "old_price": "",
            "currency": "RUB", "color": _color(p), "sizes": "",
            "gender": "женский",
            "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
            "image": _image_url(nm), "in_stock": "true",
            "parsed_at": date.today().isoformat(),
        })
        if len(rows) >= limit:
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", help="только один бренд (query из BRANDS или произвольный)")
    ap.add_argument("--limit", type=int, default=40, help="товаров на бренд")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--debug", action="store_true", help="дамп структуры ответа WB")
    args = ap.parse_args()

    targets = BRANDS
    if args.brand:
        found = [b for b in BRANDS if b["brand"].lower() == args.brand.lower()]
        targets = found or [{"query": args.brand, "brand": args.brand, "style": ""}]

    all_rows: list[dict] = []
    for cfg in targets:
        print(f"→ {cfg['brand']} …", file=sys.stderr)
        rows = fetch_brand(cfg, args.limit, debug=args.debug)
        print(f"  собрано {len(rows)}", file=sys.stderr)
        all_rows.extend(rows)
        time.sleep(1)  # вежливо к WB

    if not all_rows:
        print("Ничего не собрано — проверь --debug (структура ответа WB могла измениться).",
              file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "name", "brand", "category", "price", "old_price", "currency",
            "color", "sizes", "gender", "url", "image", "in_stock", "parsed_at"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(all_rows)
    print(f"✓ {len(all_rows)} товаров → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
