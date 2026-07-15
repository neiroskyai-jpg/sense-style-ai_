"""Скрейпер Wildberries под каталог капсулы (одежда + ОБУВЬ + сумки).

Тянет товары через публичный search-API WB и строит CSV в формате core/catalog.py (parse_csv).

ПОЧЕМУ WB: у карточек есть предметные фото (packshot) И покупаемые ссылки (+ партнёрка WB).
У Lichi/Ushatava фото на моделях, узкие стили и НЕТ обуви — слот «Обувь» в капсуле пустовал.
Lamoda не парсим: сайт отдаёт 403 (анти-бот). Для неё правильный путь — партнёрский XML-фид
(Lamoda CPA / Admitad), под него уже готов core/catalog.py::parse_feed.

ДВА РЕЖИМА ПОИСКА:
- по КАТЕГОРИЯМ (CATEGORIES) — закрывает слоты капсулы: обувь, сумки, верхняя одежда, верх, низ;
- по БРЕНДАМ (BRANDS) — quiet-luxury наводка фаундера, держит эстетику.

ЗАПУСК (нужна сеть; WB режет по частоте — между запросами пауза, есть ретраи):
    python scripts/scrape_wb.py                       # категории + бренды → products_wb.csv
    python scripts/scrape_wb.py --only shoes          # только обувь
    python scripts/scrape_wb.py --limit 40 --no-images  # быстрее, без детекции packshot

ХРУПКОСТЬ WB (проверять при поломке):
- версия search-API: v4 живой, v5 отдаёт 429. Если v4 умрёт — перебрать _SEARCH_URLS.
- при частых запросах WB отвечает HTML «429» вместо JSON → _get() ретраит с backoff.
- basket-хост картинки строится по формуле от nm; для новых nm таблица хостов расширяется.
"""
from __future__ import annotations
import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "fashion-base" / "products_wb.csv"

# Категории под слоты капсулы. Запросы — «носибельные» формулировки канона (base-vs-trend.md):
# лаконичный крой, устойчивый каблук; без страз/потёртостей — их отсекает _CANON_STOP.
CATEGORIES = [
    {"slot": "shoes",  "query": "ботильоны женские кожаные на устойчивом каблуке", "style": "classic drama"},
    {"slot": "shoes",  "query": "лоферы женские кожаные",                          "style": "classic natural"},
    {"slot": "shoes",  "query": "сапоги женские кожаные высокие",                  "style": "classic drama"},
    {"slot": "shoes",  "query": "туфли лодочки женские кожаные",                   "style": "classic"},
    {"slot": "shoes",  "query": "кроссовки женские минималистичные белые",         "style": "natural"},
    {"slot": "shoes",  "query": "челси женские кожаные",                           "style": "natural classic"},
    {"slot": "bags",   "query": "сумка женская кожаная багет",                     "style": "classic drama"},
    {"slot": "bags",   "query": "сумка шоппер женская кожаная",                    "style": "natural classic"},
    {"slot": "bags",   "query": "сумка кросс-боди женская кожаная",                "style": "classic"},
    {"slot": "outer",  "query": "пальто женское прямое шерстяное",                 "style": "classic"},
    {"slot": "outer",  "query": "пуховик женский объемный однотонный",             "style": "natural"},
    {"slot": "outer",  "query": "дубленка женская овчина",                         "style": "natural"},
    {"slot": "outer",  "query": "жакет женский прямой шерстяной",                  "style": "classic"},
    {"slot": "top",    "query": "рубашка женская хлопок оверсайз",                 "style": "classic natural"},
    {"slot": "top",    "query": "джемпер женский шерсть однотонный",               "style": "natural classic"},
    {"slot": "top",    "query": "водолазка женская шерсть",                        "style": "classic"},
    {"slot": "bottom", "query": "брюки женские прямые со стрелками",               "style": "classic"},
    {"slot": "bottom", "query": "джинсы женские прямые классические",              "style": "natural"},
    {"slot": "bottom", "query": "юбка женская миди сатин",                         "style": "romance classic"},
]

# Бренды quiet-luxury (память wb-brands-quiet-luxury). style — стилевой тег метода для скоринга.
BRANDS = [
    {"query": "Lime",           "brand": "Lime",          "style": "classic"},
    {"query": "Mollis",         "brand": "Mollis",        "style": "classic"},
    {"query": "Studio 29",      "brand": "Studio 29",     "style": "classic natural"},
    {"query": "To Be Blossom",  "brand": "To Be Blossom", "style": "natural romance"},
    {"query": "Zarina Premium", "brand": "Zarina",        "style": "classic"},
]

# Обувные бренды с эстетикой метода (классика/натуральный, кожа, лаконичный крой). Ищем по бренду
# в WB — точнее, чем родовой запрос «ботильоны женские» (меньше масс-маркет-мусора). Сайты этих
# брендов по одному закрыты анти-ботом/robots — WB-API единый рабочий канал. Слот проставит
# _category по имени (ботильоны/лоферы/сапоги → «обувь»).
SHOE_BRANDS = [
    {"query": "Portal обувь женская",         "brand": "Portal",         "style": "classic natural"},
    {"query": "Ekonika ботильоны женские",    "brand": "Ekonika",        "style": "classic"},
    {"query": "Thomas Munz туфли женские",    "brand": "Thomas Munz",    "style": "classic"},
    {"query": "Mario Berlucci женские",       "brand": "Mario Berlucci", "style": "classic drama"},
    {"query": "Respect обувь женская",        "brand": "Respect",        "style": "classic"},
    {"query": "Ralf Ringer женские",          "brand": "Ralf Ringer",    "style": "natural"},
    {"query": "Tervolina лоферы женские",     "brand": "Tervolina",      "style": "natural classic"},
    {"query": "Alba туфли женские",           "brand": "Alba",           "style": "classic romance"},
]

# Канон «Алгоритмы имиджа»: устаревшее и НЕ-базовое в капсулу не берём (base-vs-trend.md),
# плюс отсекаем бельё/домашнее/детское, которое WB подмешивает в выдачу.
_CANON_STOP = (
    "стразы", "стразами", "потертост", "потёртост", "бахром", "рваны", "пайетк", "рюкзак",
    "скинни", "3/4", "угги высокие", "дутики длинные", "пижам", "подъюбник", "халат",
    "бра ", "бюстгальтер", "трусы", "купальник", "колготк", "носки", "детск", "мужск",
    "для девочек", "для мальчиков", "школьн",
)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
# v5 отдаёт 429, v13 — другой формат. Рабочий — v4 (проверено 2026-07-14).
_SEARCH_URLS = [
    "https://search.wb.ru/exactmatch/ru/common/v4/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
]
_SEARCH_PARAMS = {
    "appType": "1", "curr": "rub", "dest": "-1257786",
    "resultset": "catalog", "sort": "popular", "spp": "30",
}

# Порог качества: WB полон безымянного шлака. Берём то, что реально носибельно.
MIN_RATING = 4.4       # рейтинг товара
MIN_FEEDBACKS = 10     # отзывов — иначе непроверенная карточка
MIN_PRICE = 1500       # ниже — почти всегда синтетика сомнительного качества

# категории метода из ключевых слов названия (нужно для _capsule_slot и match_products)
_CAT_KW = [
    ("обувь",     ["ботильон", "лофер", "сапог", "туфли", "лодочк", "кроссовк", "кед", "челси",
                   "ботинк", "балетк", "мюли", "босоножк", "угги", "слипон", "броги"]),
    ("аксессуар", ["сумка", "сумочка", "шоппер", "шопер", "клатч", "ремень", "пояс", "платок",
                   "шарф", "очки", "перчатки"]),
    ("пальто",    ["пальто", "тренч", "шуба", "дублёнка", "дубленка", "пуховик", "куртка", "плащ"]),
    ("пиджак",    ["жакет", "блейзер", "пиджак", "блэйзер"]),
    ("платье",    ["платье", "сарафан"]),
    ("юбка",      ["юбка"]),
    ("брюки",     ["брюки", "палаццо", "джинсы", "штаны", "легинсы", "кюлоты"]),
    ("трикотаж",  ["свитер", "джемпер", "кардиган", "водолазка", "пуловер", "трикотаж", "лонгслив"]),
    ("рубашка",   ["рубашка", "блуза", "сорочка"]),
    ("топ",       ["топ", "футболка", "майка", "боди"]),
]


def _category(name: str) -> str:
    low = name.lower()
    for cat, kws in _CAT_KW:
        if any(k in low for k in kws):
            return cat
    return "одежда"


def _canon_ok(name: str) -> bool:
    """Отсечь то, чего по канону не может быть в капсуле (устаревшее, бельё, не женское)."""
    low = (name or "").lower()
    return not any(s in low for s in _CANON_STOP)


def _basket_host(nm: int) -> str:
    """basket-хост картинки WB по номенклатуре. Таблица периодически расширяется —
    при 404 на новых nm добавить диапазон (последний else — самый свежий)."""
    vol = nm // 100000
    ranges = [(143, "01"), (287, "02"), (431, "03"), (719, "04"), (1007, "05"), (1061, "06"),
              (1115, "07"), (1169, "08"), (1313, "09"), (1601, "10"), (1655, "11"), (1919, "12"),
              (2045, "13"), (2189, "14"), (2405, "15"), (2621, "16"), (2837, "17"), (3053, "18"),
              (3269, "19"), (3485, "20"), (3701, "21"), (3917, "22"), (4133, "23"), (4349, "24"),
              (4565, "25"), (4877, "26"), (5189, "27"), (5501, "28"), (5813, "29")]
    for hi, host in ranges:
        if vol <= hi:
            return host
    return "30"  # свежие товары — самый новый basket; проверить вживую


def _image_url(nm: int) -> str:
    vol = nm // 100000
    part = nm // 1000
    return f"https://basket-{_basket_host(nm)}.wbbasket.ru/vol{vol}/part{part}/{nm}/images/big/1.webp"


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


def _in_stock(p: dict) -> bool:
    """Есть ли хоть один размер в наличии — иначе вещь нельзя купить, и в капсуле ей не место."""
    for s in (p.get("sizes") or []):
        if s.get("stocks"):
            return True
    return not p.get("sizes")  # обувь/сумки без размерной сетки считаем доступными


def _color(p: dict) -> str:
    cols = p.get("colors") or []
    if cols and isinstance(cols[0], dict):
        return cols[0].get("name", "")
    return ""


def _get(query: str, retries: int = 4) -> list[dict]:
    """Запрос к search-API с ретраями: при частых обращениях WB отдаёт HTML «429» вместо JSON."""
    for attempt in range(retries):
        for url in _SEARCH_URLS:
            try:
                r = requests.get(url, params={**_SEARCH_PARAMS, "query": query},
                                 headers=_HEADERS, timeout=30)
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    return (r.json().get("data") or {}).get("products") or []
            except Exception:  # noqa: BLE001 — сеть/JSON: пробуем следующий url/попытку
                pass
        time.sleep(2 * (attempt + 1))  # backoff: WB отпускает через несколько секунд
    print(f"  ! WB не ответил по запросу «{query}»", file=sys.stderr)
    return []


def _detect_kind(rows: list[dict], workers: int = 8) -> None:
    """Проставить image_kind (packshot | model): в капсуле нужна сама вещь, а не съёмка на модели.
    Считаем долю «кожи» в кадре (та же эвристика, что в scripts/packshot.py). Мутирует rows."""
    try:
        from scripts.packshot import _skin_fraction  # переиспользуем детектор
        from PIL import Image
        import io
    except Exception:  # noqa: BLE001 — нет Pillow → оставляем kind пустым, капсула всё равно соберётся
        return

    def one(row: dict) -> None:
        try:
            r = requests.get(row["image"], headers=_HEADERS, timeout=10)
            if r.status_code != 200:
                return
            img = Image.open(io.BytesIO(r.content)).convert("RGB").resize((160, 160))
            row["image_kind"] = "model" if _skin_fraction(img) > 0.04 else "packshot"
        except Exception:  # noqa: BLE001 — картинка не открылась: не блокируем сбор
            pass

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, rows))


def _rating(p: dict) -> float:
    """Рейтинг: WB зовёт его по-разному в разных версиях API — берём первое непустое."""
    for key in ("rating", "reviewRating", "nmReviewRating", "supplierRating"):
        if p.get(key):
            return float(p[key])
    return 0.0


def _feedbacks(p: dict) -> int:
    for key in ("feedbacks", "nmFeedbacks"):
        if p.get(key):
            return int(p[key])
    return 0


# Счётчик причин отсева: если прогон вернул 0 товаров, сразу видно, какой фильтр всё съел
# (или что WB переименовал поля рейтинга). Без этого пришлось бы гадать.
REJECTED: dict[str, int] = {}


def _reject(reason: str) -> None:
    REJECTED[reason] = REJECTED.get(reason, 0) + 1


def _row(p: dict, style: str, brand_override: str = "") -> dict | None:
    nm, name = p.get("id"), (p.get("name") or "").strip()
    if not nm or not name:
        return _reject("без имени/id") or None
    if not _canon_ok(name):
        return _reject("стоп-лист канона (устаревшее/бельё/не женское)") or None
    if _rating(p) < MIN_RATING:
        return _reject(f"рейтинг < {MIN_RATING}") or None
    if _feedbacks(p) < MIN_FEEDBACKS:
        return _reject(f"отзывов < {MIN_FEEDBACKS}") or None
    price = _price(p)
    if price < MIN_PRICE:
        return _reject(f"цена < {MIN_PRICE} ₽") or None
    if not _in_stock(p):
        return _reject("нет в наличии") or None
    return {
        "id": str(nm), "name": name,
        "brand": brand_override or (p.get("brand") or "").strip(),
        "category": _category(name), "price": price, "old_price": "",
        "currency": "RUB", "color": _color(p), "sizes": "", "gender": "женский",
        "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        "image": _image_url(nm), "in_stock": "true", "image_kind": "",
        "style_fields": style, "parsed_at": date.today().isoformat(),
    }


def fetch_category(cfg: dict, limit: int) -> list[dict]:
    rows = []
    for p in _get(cfg["query"]):
        row = _row(p, cfg["style"])
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def fetch_brand(cfg: dict, limit: int) -> list[dict]:
    rows, want = [], cfg["brand"].lower()
    for p in _get(cfg["query"]):
        if want not in (p.get("brand") or "").lower():
            continue  # WB подмешивает смежные бренды — оставляем только целевой
        row = _row(p, cfg["style"], brand_override=cfg["brand"])
        if row:
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def main() -> int:
    global MIN_RATING, MIN_FEEDBACKS, MIN_PRICE  # noqa: PLW0603 — пороги качества настраиваются с CLI
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["shoes", "bags", "outer", "top", "bottom", "brands",
                                        "shoe-brands"],
                    help="собрать только один слот, только бренды одежды или только обувные бренды")
    ap.add_argument("--limit", type=int, default=12, help="товаров на запрос")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-images", action="store_true", help="не определять packshot/model (быстрее)")
    ap.add_argument("--min-rating", type=float, default=MIN_RATING)
    ap.add_argument("--min-feedbacks", type=int, default=MIN_FEEDBACKS)
    ap.add_argument("--min-price", type=int, default=MIN_PRICE)
    args = ap.parse_args()
    MIN_RATING, MIN_FEEDBACKS, MIN_PRICE = args.min_rating, args.min_feedbacks, args.min_price

    slot_categories = {"shoes", "bags", "outer", "top", "bottom"}
    all_rows: list[dict] = []

    # поиск по категориям (родовые запросы под слоты капсулы)
    if args.only is None or args.only in slot_categories:
        cats = [c for c in CATEGORIES if not args.only or c["slot"] == args.only]
        for cfg in cats:
            rows = fetch_category(cfg, args.limit)
            print(f"→ [{cfg['slot']}] {cfg['query'][:40]:42} {len(rows):3}", file=sys.stderr)
            all_rows.extend(rows)
            time.sleep(1.5)  # вежливо к WB: без паузы прилетает 429

    # поиск по брендам: одежда (BRANDS) и/или обувь (SHOE_BRANDS)
    brand_sets = []
    if args.only in (None, "brands"):
        brand_sets.append(BRANDS)
    if args.only in (None, "shoes", "shoe-brands"):
        brand_sets.append(SHOE_BRANDS)
    for cfgs in brand_sets:
        for cfg in cfgs:
            rows = fetch_brand(cfg, args.limit)
            print(f"→ [бренд] {cfg['brand']:44} {len(rows):3}", file=sys.stderr)
            all_rows.extend(rows)
            time.sleep(1.5)

    # один товар попадает в разные запросы («ботильоны» и бренд) → схлопываем по id
    uniq: dict[str, dict] = {}
    for r in all_rows:
        uniq.setdefault(r["id"], r)
    rows = list(uniq.values())
    if REJECTED:
        print("отсеяно:", ", ".join(f"{k} — {v}" for k, v in
                                    sorted(REJECTED.items(), key=lambda kv: -kv[1])), file=sys.stderr)
    if not rows:
        print("\nНичего не собрано. Смотри строку «отсеяно» выше:\n"
              "  • всё съели фильтры → ослабь: --min-rating 4.0 --min-feedbacks 0 --min-price 1000\n"
              "  • отсеяно пусто и запросы вернули 0 → WB не отдал данные (429 или сменил API):\n"
              "    запусти с домашнего IP, не из облака; при 429 подожди 5-10 минут.",
              file=sys.stderr)
        return 1

    if not args.no_images:
        print(f"… определяю packshot/model для {len(rows)} фото", file=sys.stderr)
        _detect_kind(rows)
    packshots = sum(1 for r in rows if r.get("image_kind") == "packshot")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "name", "brand", "category", "price", "old_price", "currency", "color", "sizes",
            "gender", "url", "image", "in_stock", "image_kind", "style_fields", "parsed_at"]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"✓ {len(rows)} товаров ({packshots} packshot) → {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
