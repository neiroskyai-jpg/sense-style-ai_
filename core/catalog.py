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

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "brand": self.brand,
            "category": self.category, "price": self.price, "old_price": self.old_price,
            "currency": self.currency, "color": self.color, "sizes": self.sizes,
            "gender": self.gender, "url": self.url, "image": self.image,
            "in_stock": self.in_stock,
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

    scored: list[tuple[float, Product]] = []
    for p in products:
        if not p.in_stock:
            continue
        if p.gender and p.gender.lower() not in ("женский", "female", "women", "ж"):
            continue
        hay = f"{p.name} {p.category} {p.color}".lower()
        if any(t and t in hay for t in stop):       # табу-цвет/деталь — вон
            continue
        if any(a and a in hay for a in avoid):       # антипаттерн фигуры — вон
            continue

        score = 0.0
        if any(c and c in p.color.lower() for c in palette):
            score += 2.0
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
