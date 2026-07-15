"""Каталог партнёрки (Lamoda CPA / Admitad XML-фид) → нормализация → подбор под профиль.

Standalone-модуль: НЕ импортируется в пайплайн/веб, пока не подключим реальный фид.
Когда придёт ссылка на XML-фид Lamoda — проверяем маппинг полей в `_FIELD_TAGS`
(скорее всего формат YML: <offers><offer>…), при необходимости правим только маппинг.

Без внешних зависимостей (stdlib xml.etree) — не трогаем requirements и сборку Amvera.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Product:
    """Нормализованная вещь каталога (единый вид независимо от формата фида)."""
    id: str
    name: str
    brand: str = ""
    category: str = ""
    price: float = 0.0
    old_price: float = 0.0
    currency: str = "RUB"
    color: str = ""
    sizes: list[str] = field(default_factory=list)
    gender: str = ""
    url: str = ""          # партнёрский deeplink
    image: str = ""
    in_stock: bool = True
    style_fields: str = ""  # стилевые поля метода (classic/natural/drama/romance), напр. из бренда
    image_kind: str = ""    # packshot (вещь без модели) | model (съёмка на модели)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "brand": self.brand,
            "category": self.category, "price": self.price, "old_price": self.old_price,
            "currency": self.currency, "color": self.color, "sizes": self.sizes,
            "gender": self.gender, "url": self.url, "image": self.image,
            "in_stock": self.in_stock, "style_fields": self.style_fields,
            "image_kind": self.image_kind,
        }


# Возможные имена тегов/параметров под одно нормализованное поле (правится под реальный фид).
_FIELD_TAGS = {
    "name": ["name", "model", "title"],
    "brand": ["vendor", "brand", "manufacturer"],
    "price": ["price"],
    "old_price": ["oldprice", "old_price"],
    "currency": ["currencyId", "currency"],
    "url": ["url", "link", "deeplink"],
    "image": ["picture", "image", "img"],
}
# Параметры, которые в YML лежат как <param name="...">значение</param>
_PARAM_KEYS = {
    "category": ["категория", "category", "тип", "type"],
    "color": ["цвет", "color", "основной цвет"],
    "gender": ["пол", "gender", "sex"],
    "size": ["размер", "size", "размеры"],
}


def parse_feed(source: str | Path) -> list[Product]:
    """XML-фид (путь, URL-строка с XML или сам XML-текст) → список Product.

    Толерантен к пропускам. Понимает YML-подобную структуру (<offer>) и общую (<item>/<product>).
    """
    xml_text = _read_source(source)
    root = ET.fromstring(xml_text)

    offers = (root.findall(".//offer") or root.findall(".//item")
              or root.findall(".//product"))
    products: list[Product] = []
    for el in offers:
        p = _offer_to_product(el)
        if p is not None:
            products.append(p)
    return products


def parse_csv(source: str | Path) -> list[Product]:
    """CSV-файл (выгрузка парсера/пилот/фид без XML) → список Product.

    Ожидает колонки по именам полей Product (id,name,brand,category,price,old_price,
    color,sizes,gender,url,image,in_stock). Лишние колонки (parsed_at и т.п.) игнорит,
    отсутствующие — берёт по умолчанию. Единый вход с parse_feed: дальше тот же match_products.
    """
    import csv as _csv
    path = Path(source)
    rows = list(_csv.DictReader(path.open(encoding="utf-8-sig")))
    products: list[Product] = []
    for i, r in enumerate(rows):
        name = (r.get("name") or r.get("product_name") or "").strip()
        pid = (r.get("id") or r.get("sku") or "").strip() or name or str(i)
        if not name:
            continue
        sizes_raw = (r.get("sizes") or "").replace(";", ",")
        in_stock = str(r.get("in_stock", "true")).strip().lower() not in ("false", "0", "no", "нет")
        products.append(Product(
            id=str(pid), name=name,
            brand=(r.get("brand") or "").strip(),
            category=(r.get("category") or "").strip(),
            price=_to_float(r.get("price")),
            old_price=_to_float(r.get("old_price")),
            currency=(r.get("currency") or "RUB").strip(),
            color=(r.get("color") or "").strip(),
            sizes=[s.strip() for s in sizes_raw.split(",") if s.strip()],
            gender=(r.get("gender") or "").strip(),
            url=(r.get("url") or r.get("link") or "").strip(),
            image=(r.get("image") or r.get("image_url") or "").strip(),
            in_stock=in_stock,
            image_kind=(r.get("image_kind") or "").strip(),
            # стилевые поля метода: у WB-фида проставляет парсер (у бренда их может не быть
            # в brands.csv). Без этого вещь не участвует в скоринге по подстилю.
            style_fields=(r.get("style_fields") or "").strip(),
        ))
    return products


def products_to_csv(products: list[Product], dest: str | Path) -> Path:
    """Сохранить список Product в CSV (колонки = поля Product). Для пилота/отладки."""
    import csv as _csv
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cols = ["id", "name", "brand", "category", "price", "old_price", "currency",
            "color", "sizes", "gender", "url", "image", "in_stock"]
    with dest.open("w", newline="", encoding="utf-8-sig") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        for p in products:
            d = p.as_dict()
            d["sizes"] = ";".join(p.sizes)
            w.writerow([d[c] for c in cols])
    return dest


def _read_source(source: str | Path) -> str:
    s = str(source)
    if s.lstrip().startswith("<"):       # уже XML-текст
        return s
    path = Path(source)
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"Фид не найден и не похож на XML: {s[:80]}")


def _offer_to_product(el: ET.Element) -> Product | None:
    get = lambda keys: _first_tag(el, keys)
    params = _params(el)

    name = get(_FIELD_TAGS["name"]) or ""
    pid = el.get("id") or el.get("sku") or get(["id", "sku"]) or name
    if not pid:
        return None

    available = el.get("available")
    in_stock = (available is None) or (str(available).lower() in ("true", "1", "yes"))

    return Product(
        id=str(pid),
        name=name,
        brand=get(_FIELD_TAGS["brand"]) or "",
        category=get(["categoryId", "category"]) or _param(params, "category"),
        price=_to_float(get(_FIELD_TAGS["price"])),
        old_price=_to_float(get(_FIELD_TAGS["old_price"])),
        currency=get(_FIELD_TAGS["currency"]) or "RUB",
        color=_param(params, "color"),
        sizes=[s for s in (_param(params, "size") or "").replace(";", ",").split(",") if s.strip()],
        gender=_param(params, "gender"),
        url=get(_FIELD_TAGS["url"]) or "",
        image=get(_FIELD_TAGS["image"]) or "",
        in_stock=in_stock,
    )


def _first_tag(el: ET.Element, names: list[str]) -> str:
    for n in names:
        child = el.find(n)
        if child is not None and (child.text or "").strip():
            return child.text.strip()
    return ""


def _params(el: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in el.findall("param"):
        key = (p.get("name") or "").strip().lower()
        if key:
            out[key] = (p.text or "").strip()
    return out


def _param(params: dict[str, str], field_name: str) -> str:
    for key in _PARAM_KEYS[field_name]:
        if key in params:
            return params[key]
    return ""


def _to_float(s: str) -> float:
    try:
        return float(str(s).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return 0.0


# ── Подбор под профиль клиентки ───────────────────────────────────────────────
# Стартовый ruleset под срез «прямоугольник × Натуральный». Расширяется по методу/RAG.

# Категории, релевантные базовому стилю (грубая разметка, уточняется по фиду/методу).
_FORMULA_CATEGORIES = {
    "natural": ["брюки", "трикотаж", "джемпер", "кардиган", "рубашка", "жакет",
                "юбка", "платье", "лоферы", "ботинки", "тренч"],
}
# Силуэты/категории, которые для прямоугольника работают плохо (заглушка, уточнить по методу).
_FIGURE_AVOID = {
    "rectangle": ["футляр", "балахон", "оверсайз-без-пояса"],
}

# Цвето-семьи: имена палитры метода («Чёрная ночь», «Холодный тауп», «Рубиновый») и сырые цвета
# фида («чёрный», «тауп», «винный») сводятся к одной базовой семье — иначе подбор по подстроке
# не находит совпадений и капсула получается случайной.
_COLOR_FAMILY_KW = {
    "black":  ["чёрн", "черн", "графит", "антрацит", "уголь", "вороно", "ночь"],
    "white":  ["бел", "кипен", "молочн", "айвори", "слонов", "экрю", "снежн"],
    "grey":   ["сер", "стальн", "тауп", "дымч", "пепельн", "мышин"],
    "beige":  ["беж", "песочн", "кремов", "крем", "верблюж", "карамель", "нюд", "латте", "камел"],
    "brown":  ["коричн", "шоколад", "кофе", "какао", "табач", "каштан", "мокко"],
    "blue":   ["син", "джинс", "деним", "индиго", "навы", "кобальт", "лазурн", "голуб", "электрик", "васильков"],
    "green":  ["зелён", "зелен", "хвой", "изумруд", "олив", "милитари", "фисташ", "келли", "малахит"],
    "red":    ["красн", "руб", "бордо", "бордов", "вишн", "алый", "марсал", "терракот", "кирпичн", "клюкв", "гранат"],
    "pink":   ["розов", "пудр", "фукси", "коралл", "пыльн", "фламинго", "чайн"],
    "purple": ["фиолет", "сирен", "лаванд", "пурпур", "баклажан", "ежевич", "сливов", "орхиде", "виноград"],
    "yellow": ["жёлт", "желт", "горчич", "золот", "лимонн", "янтарн", "медов"],
    "orange": ["оранж", "апельсин", "манго", "тыкв", "морков"],
    "teal":   ["бирюз", "аквамарин", "мятн"],
}


def _color_families(text: str) -> set[str]:
    """Текст (цвет и/или название вещи/палитры) → множество базовых цвето-семей."""
    t = (text or "").lower()
    return {fam for fam, kws in _COLOR_FAMILY_KW.items() if any(k in t for k in kws)}


def _style_set(text: str) -> set[str]:
    """Строка стилевых полей («classic; natural» / «drama romance») → множество кодов стиля."""
    return {s.strip().lower() for s in (text or "").replace(",", ";").replace(" ", ";").split(";")
            if s.strip()}


def match_products(profile: dict, products: list[Product], k: int = 12) -> list[Product]:
    """Профиль диагностики + каталог → топ-k подходящих вещей (с объяснимым скором).

    profile: {figure_type, base_style, palette:[{name}], stop_list:[...], price_max, gender}
    Логика прозрачная и правится: цвет из палитры +, табу-цвет — исключение, категория под формулу +,
    бюджет, наличие, женский пол. Это каркас — точность растёт по мере разметки и связки с RAG.
    """
    palette = _names(profile.get("palette"))
    stop = _names(profile.get("stop_list"))
    base = (profile.get("base_style") or "").lower()
    figure = (profile.get("figure_type") or "").lower()
    price_max = profile.get("price_max") or 0
    good_cats = _FORMULA_CATEGORIES.get(base, [])
    avoid = _FIGURE_AVOID.get(figure, [])
    # цвето-семьи палитры и стоп-цветов (имена метода → базовые семьи)
    palette_fams = set().union(*(_color_families(c) for c in palette)) if palette else set()
    stop_fams = set().union(*(_color_families(c) for c in stop)) if stop else set()
    client_styles = _style_set(" ".join(profile.get("styles") or []))  # доминанты клиентки

    scored: list[tuple[float, Product]] = []
    for p in products:
        if not p.in_stock:
            continue
        if p.gender and p.gender.lower() not in ("женский", "female", "women", "ж"):
            continue
        hay = f"{p.name} {p.category} {p.color}".lower()
        if any(t and t in hay for t in stop):       # табу-деталь/цвет по подстроке — вон
            continue
        if any(a and a in hay for a in avoid):       # антипаттерн фигуры — вон
            continue
        prod_fams = _color_families(f"{p.color} {p.name}")
        if stop_fams and (prod_fams & stop_fams):     # цвет из стоп-семьи (напр. горчичный) — вон
            continue

        score = 0.0
        if palette_fams and (prod_fams & palette_fams):        # цвет в палитре — сильный плюс
            score += 2.0
        elif palette_fams and prod_fams:                       # есть цвет, но вне палитры — минус
            score -= 1.5
        prod_styles = _style_set(p.style_fields)               # стиль вещи (наследует от бренда)
        if client_styles and prod_styles:
            if client_styles & prod_styles:                    # стиль вещи совпал с доминантой — плюс
                score += 1.5
            else:                                              # стиль мимо (напр. drama на классике) — минус
                score -= 1.0
        if any(c and c in hay for c in good_cats):
            score += 1.5
        if price_max and p.price and p.price <= price_max:
            score += 0.5
        if p.old_price and p.price and p.price < p.old_price:  # на распродаже — небольшой буст
            score += 0.3
        scored.append((score, p))

    scored.sort(key=lambda sp: sp[0], reverse=True)
    return [p for _, p in scored[:k]]


def _names(items) -> list[str]:
    """[{name}], ['строка', …] или None → список строк в нижнем регистре."""
    out: list[str] = []
    for it in items or []:
        name = it.get("name") if isinstance(it, dict) else it
        if name:
            out.append(str(name).lower())
    return out
