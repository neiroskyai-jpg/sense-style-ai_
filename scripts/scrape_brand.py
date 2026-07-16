# -*- coding: utf-8 -*-
"""Пилот-скрейпер бренд-сайтов → CSV в формате core.catalog.Product.

Конфиг-управляемый: на бренд — базовый URL, категории (наш слот + URL), CSS-селекторы.
Выход — CSV с колонками Product (id,name,brand,category,price,color,url,image,...),
который читается core.catalog.parse_csv и идёт в match_products. Ноль дублирования модели.

Запуск:
    python -m scripts.scrape_brand --brand ushatava --limit 20
    python -m scripts.scrape_brand --brand ushatava --limit 30 --out data/fashion-base/products_ushatava.csv

Только женские категории. requests не обязателен — используется stdlib urllib.
Сайты бывают на JS/антиботе — если категория пустая, парсить у себя (полная сеть) или через Playwright.
"""
from __future__ import annotations
import argparse, csv, json, re, ssl, time, urllib.request
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.packshot import pick_packshot

ROOT = Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# ── Конфиги брендов. mode: "html" (CSS) | "next_json" (__NEXT_DATA__). ──
SITES = {
    # Ushatava (Bitrix, HTML) — селекторы проверены вживую 2026-07-01.
    "ushatava": dict(
        mode="html",
        base="https://ushatava.ru",
        brand="Ushatava",
        categories={
            "пиджак":  "/store/w/catalog/pidzhaki/",
            "платье":  "/store/w/catalog/platya/",
            "брюки":   "/store/w/catalog/bryuki-i-dzhinsy/",
            "рубашка": "/store/w/catalog/rubashki/",
            "юбка":    "/store/w/catalog/yubki/",
            "топ":     "/store/w/catalog/topy-i-korsety/",
            "трикотаж":"/store/w/catalog/dzhempery-i-kardigany/",
        },
        card=".product-card",
        name=".name",
        price=".price-wrap",
        color=".product-colors .color",   # цвет как style="--product-color:#hex"
        gender="женский",
    ),
    # Lichi (Next.js) — товары в __NEXT_DATA__ → catalogData.aProduct. Проверено вживую 2026-07-16.
    # Категории — всё дерево сайта (pageProps.categoriesData), кроме купальников: слота в капсуле
    # у них нет. Страница отдаёт 12 товаров (iLimit) и пагинация живёт в приватном API — поэтому
    # ширину берём числом категорий, а не глубиной листинга. Это и полезнее: закрывает слоты
    # (юбки/брюки/жакеты/верхняя), а не приносит 60 платьев.
    "lichi": dict(
        mode="next_json", base="https://lichi.com", brand="Lichi", gender="женский",
        # Порядок значим: одна вещь лежит у Lichi в нескольких разделах (жакет от костюма — и в
        # «жакет», и в «комплект», и в «одежда»). Дедуп в main() оставляет ПЕРВУЮ встреченную
        # категорию, поэтому конкретные идут раньше общих: жакету нужна категория «жакет», иначе
        # match_products не найдёт его по слоту. «комплект» — только то, что продаётся целиком,
        # «одежда» — остаток, не попавший ни в один конкретный раздел.
        categories={
            "платье":     "/ru/ru/category/dresses",
            "блуза":      "/ru/ru/category/blouses_tops",
            "брюки":      "/ru/ru/category/trousers_jeans",
            "юбка":       "/ru/ru/category/skirts",
            "трикотаж":   "/ru/ru/category/sweaters_sweatshirts",
            "джинсы":     "/ru/ru/category/denim",
            "жакет":      "/ru/ru/category/jackets",
            "пальто":     "/ru/ru/category/outerwear",
            "шорты":      "/ru/ru/category/shorts",
            "комбинезон": "/ru/ru/category/overalls",
            "аксессуар":  "/ru/ru/category/accessory",
            "комплект":   "/ru/ru/category/sets",
            "одежда":     "/ru/ru/category/clothes",
        },
    ),
    # Charmstore — из этой среды не резолвился; заготовка, уточнить селекторы у себя.
    "charmstore": dict(mode="html", base="https://charmstore.ru", brand="Charmstore", gender="женский",
                       categories={}, card=".product-item", name=".product-name",
                       price=".product-price", color=""),
}

# ── hex → имя цвета (ближайший из палитры метода), для сайтов, дающих только hex ──
NAMED_COLORS = {
    "чёрный": "000000", "белый": "ffffff", "серый": "808080", "графит": "3a3a3c",
    "тёмно-синий": "1f2a44", "серо-синий": "5b6b7c", "голубой": "9fc0e0", "синий": "2a4b8d",
    "тауп": "8a7f72", "какао": "6b5544", "шоколад": "3f2a20", "коричневый": "6b4a2f",
    "беж": "d8c4a8", "кремовый": "f2e6cf", "молочный": "f6efe7", "айвори": "efe7d3",
    "оливковый": "6b6a3a", "хаки": "8a815a", "горчица": "c8992e", "терракота": "b5643c",
    "коралл": "e9765b", "красный": "c0392b", "бордовый": "5e2028", "винный": "6d2233",
    "розовый": "e7a9b8", "пыльная роза": "c99aa2", "лавандовый": "b7a9cf", "сливовый": "6b3f5b",
    "фиолетовый": "6a4a8c", "изумруд": "1f6b52", "зелёный": "3f7a52", "бирюза": "3fae9f",
    "жёлтый": "e6c34a", "оранжевый": "d4772e", "серебристый": "c2c6cb",
}
def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4)) if len(h) >= 6 else None
def hex_to_name(h: str) -> str:
    rgb = _rgb(h)
    if not rgb:
        return h.lower()
    best, bd = h.lower(), 1e9
    for name, hx in NAMED_COLORS.items():
        r = _rgb(hx)
        d = sum((a - b) ** 2 for a, b in zip(rgb, r))
        if d < bd:
            bd, best = d, name
    return best


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=25, context=_CTX).read().decode("utf-8", "ignore")


def price_num(text: str) -> float:
    """Первая цена из текста. На распродаже .price-wrap содержит текущую+старую —
    берём ПЕРВУЮ число-группу (напр. '45 900 32 900' → 45900), а не склейку всех цифр."""
    m = re.search(r"\d{1,3}(?:[  ]\d{3})+|\d+", text or "")
    return float(re.sub(r"\D", "", m.group())) if m else 0.0


def color_from(el, cfg) -> str:
    """Ushatava кодирует цвет hex-переменной; иначе — текст селектора цвета."""
    if not cfg.get("color"):
        return ""
    node = el.select_one(cfg["color"])
    if not node:
        return ""
    style = node.get("style", "")
    m = re.search(r"--product-color:\s*(#[0-9a-fA-F]{3,6})", style)
    if m:
        return hex_to_name(m.group(1))
    return node.get_text(strip=True)


def first_image(card, base: str) -> str:
    src = card.select_one("picture source[data-srcset], img[data-src], img[src]")
    if not src:
        return ""
    val = src.get("data-srcset") or src.get("data-src") or src.get("src") or ""
    val = val.split(",")[0].strip().split(" ")[0]
    return base + val if val.startswith("/") else val


_GALLERY_IMG = re.compile(r"""["'](/upload/[^\s"'<>\\]+?\.(?:jpg|jpeg|png|webp))""", re.I)
_IBLOCK_DIR = re.compile(r"/iblock/([0-9a-z]+/[0-9a-z]+)/", re.I)
_RESIZE_W = re.compile(r"/(\d{3,4})_\d{3,4}_")


def product_gallery(url: str, base: str) -> list[str]:
    """Галерея товара со страницы (Bitrix-сайты вроде Ushatava).

    Один кадр лежит на сайте в нескольких размерах: оригинал `/upload/iblock/<dir>/x.jpg` и его
    ресайзы `/upload/resize_cache/iblock/<dir>/960_1273_x.jpg`. Группируем по `<dir>` (это и есть
    кадр) и берём САМЫЙ МЕЛКИЙ доступный размер: оригиналы тут 1920×2547, качать их только чтобы
    понять «есть ли в кадре модель» — минуты на ровном месте. Мелкий кадр и для оценки быстрее,
    и на витрине легче (а в PDF вшивается меньшим data-URL).
    """
    try:
        html = fetch(url)
    except Exception:  # noqa: BLE001 — страница не открылась → останется фото из листинга
        return []

    frames: dict[str, tuple[int, str]] = {}  # кадр (iblock-dir) → (ширина, url)
    for path in _GALLERY_IMG.findall(html):
        m = _IBLOCK_DIR.search(path)
        if not m:
            continue
        frame = m.group(1).lower()
        w = _RESIZE_W.search(path)
        width = int(w.group(1)) if w else 99999  # без ресайза = оригинал, самый крупный
        if frame not in frames or width < frames[frame][0]:
            frames[frame] = (width, base + path)
    return [url_ for _w, url_ in frames.values()]


def _scrape_html(cfg, cat, path, rows, limit, per_cat) -> int:
    base = cfg["base"]
    soup = BeautifulSoup(fetch(base + path), "html.parser")
    got = 0
    for card in soup.select(cfg["card"]):
        if len(rows) >= limit or got >= per_cat:
            break
        name_el = card.select_one(cfg["name"])
        if not name_el:
            continue
        href = card.get("href") or (card.select_one("a[href]") or {}).get("href", "")
        pid = card.get("data-product-id") or card.get("data-element-id") or href
        price_el = card.select_one(cfg["price"])
        url = (base + href) if href.startswith("/") else href
        # предметный кадр (вещь без модели) — из галереи товара; в листинге всегда съёмка на модели
        gallery = product_gallery(url, base) if url else []
        if gallery:
            img, is_pack = pick_packshot(gallery)
        else:
            img, is_pack = first_image(card, base), False
        rows.append(dict(
            id=str(pid).strip(), name=name_el.get_text(strip=True), brand=cfg["brand"],
            category=cat, price=price_num(price_el.get_text() if price_el else ""),
            old_price="", currency="RUB", color=color_from(card, cfg), sizes="",
            gender=cfg["gender"], url=url,
            image=img, image_kind="packshot" if is_pack else "model",
            in_stock="true", parsed_at=date.today().isoformat(),
        ))
        got += 1
    return got


def _scrape_next_json(cfg, cat, path, rows, limit, per_cat) -> int:
    """Next.js-сайт (Lichi): товары в <script id=__NEXT_DATA__> → catalogData.aProduct."""
    html = fetch(cfg["base"] + path)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return 0
    aprod = json.loads(m.group(1))["props"]["pageProps"].get("catalogData", {}).get("aProduct", [])
    got = 0
    for p in aprod:
        if len(rows) >= limit or got >= per_cat:
            break
        colors = p.get("colors") or {}
        cur = colors.get("current") or {}
        color = cur.get("name") or (hex_to_name(cur["value"]) if cur.get("value") else "")
        photos = p.get("photos") or []
        # берём ПРЕДМЕТНЫЙ кадр (вещь без модели), а не первый: первый у Lichi — всегда съёмка
        # на модели, а в капсуле нужна сама вещь. Нет предметного (бывает у аксессуаров) —
        # остаётся кадр на модели.
        gallery = [x.get("big") for x in photos if isinstance(x, dict) and x.get("big")]
        img, is_pack = pick_packshot(gallery)
        sizes = p.get("sizes") or {}
        size_names = [s.get("name") for s in sizes.values() if isinstance(s, dict) and s.get("name")]
        # Lichi отдаёт currency объектом оформления цены ({prefix, postfix: 'руб.', ...}), а не кодом
        # валюты — без проверки типа в CSV уезжал repr словаря вместо RUB.
        cur = p.get("currency")
        rows.append(dict(
            id=str(p.get("id", "")), name=p.get("name", ""), brand=cfg["brand"], category=cat,
            price=p.get("price") or 0, old_price=(p.get("original_price") or ""),
            currency=cur.strip() if isinstance(cur, str) and cur.strip() else "RUB",
            color=color, sizes=";".join(size_names),
            gender=cfg["gender"], url=p.get("url", ""), image=img,
            image_kind="packshot" if is_pack else "model",
            in_stock="true" if p.get("available") else "false", parsed_at=date.today().isoformat(),
        ))
        got += 1
    return got


def scrape(brand_key: str, limit: int) -> list[dict]:
    cfg = SITES[brand_key]
    rows: list[dict] = []
    if not cfg["categories"]:
        print(f"! Для {brand_key} категории не заданы — заполни SITES['{brand_key}'].")
        return rows
    handler = _scrape_next_json if cfg.get("mode") == "next_json" else _scrape_html
    per_cat = max(1, limit // len(cfg["categories"])) + 1
    for cat, path in cfg["categories"].items():
        if len(rows) >= limit:
            break
        try:
            got = handler(cfg, cat, path, rows, limit, per_cat)
        except Exception as e:
            print(f"x {cat} ({path}): {type(e).__name__} {str(e)[:80]}")
            continue
        print(f"  {cat}: +{got} (всего {len(rows)})")
        time.sleep(0.6)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brand", required=True, choices=list(SITES))
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rows = scrape(a.brand, a.limit)
    if not rows:
        print("Ничего не собрано.")
        return
    # Один товар приходит из нескольких разделов сайта → схлопываем по id, оставляя первую
    # (самую конкретную — см. порядок categories) категорию.
    uniq: dict[str, dict] = {}
    for r in rows:
        uniq.setdefault(r["id"], r)
    dropped = len(rows) - len(uniq)
    rows = list(uniq.values())
    if dropped:
        print(f"  дублей схлопнуто: {dropped}")
    out = Path(a.out) if a.out else ROOT / "data" / "fashion-base" / f"products_{a.brand}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "name", "brand", "category", "price", "old_price", "currency",
            "color", "sizes", "gender", "url", "image", "image_kind", "in_stock", "parsed_at"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    # Маркеры — ASCII: консоль Windows живёт в cp1251, и «✓»/«→» роняют финальную печать
    # UnicodeEncodeError уже ПОСЛЕ записи файла — прогон выглядит упавшим, хотя данные собраны.
    print(f"\nOK: {len(rows)} товаров -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
