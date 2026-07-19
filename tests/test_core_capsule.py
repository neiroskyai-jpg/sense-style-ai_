"""Капсула-ядро собирается ИЗ ОБРАЗОВ клиентки, а не отдельным набором из каталога.

Правило продукта (бизнес-логика тарифов, 19.07.2026): «Капсула в Карте НЕ должна быть отдельным
случайным набором одежды. Она собирается из уже сгенерированных образов и повторяющихся вещей,
которые чаще всего работают в разных сценариях». Иначе клиентка видит образы отдельно, капсулу
отдельно и не понимает, откуда та взялась.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

LOOKS = [
    {"scenario": "деловая встреча", "items": ["Жакет структурный", "Брюки прямые", "Лоферы кожаные"]},
    {"scenario": "презентация", "items": ["Жакет структурный", "Юбка миди", "Лоферы кожаные"]},
    {"scenario": "выходные", "items": ["Джемпер трикотажный", "Брюки прямые"]},
    {"scenario": "свидание", "items": ["Платье-комбинация", "Туфли-лодочки"]},
]


def test_capsule_is_built_from_look_items():
    """Каждая вещь капсулы должна встречаться в образах."""
    cap = m._core_capsule_from_looks(LOOKS, [])
    from_looks = {i.lower() for lk in LOOKS for i in lk["items"]}
    assert cap
    for it in cap:
        assert any(it["name"].lower() in n or n in it["name"].lower() for n in from_looks), it["name"]


def test_repeating_items_are_core():
    """Вещь из нескольких сценариев — ядро; из одного — акцент."""
    by_name = {i["name"]: i for i in m._core_capsule_from_looks(LOOKS, [])}
    assert by_name["Жакет структурный"]["capsule_role"] == "core"
    assert by_name["Лоферы кожаные"]["capsule_role"] == "core"
    assert by_name["Юбка миди"]["capsule_role"] == "accent"


def test_core_items_come_first():
    """Сначала то, что работает чаще — это и есть ядро гардероба."""
    cap = m._core_capsule_from_looks(LOOKS, [])
    counts = [i["outfits_count"] for i in cap]
    assert counts == sorted(counts, reverse=True)


def test_why_explains_the_link_to_looks():
    """Клиентка должна видеть, почему вещь здесь, а не гадать."""
    cap = m._core_capsule_from_looks(LOOKS, [])
    top = cap[0]
    assert "образ" in top["why"].lower()
    assert top["scenarios"] if "scenarios" in top else True


def test_catalog_only_illustrates_and_never_adds_items():
    """Каталог не добавляет вещи в капсулу и не привязывает её к товару бренда.

    Бизнес-решение 19.07.2026: продукт не должен зависеть от договорённостей с брендами, фидов,
    наличия и цен. Вещь описывается характеристиками, ссылка ведёт на ПОИСК по описанию,
    а фото из каталога — только иллюстрация типа вещи.
    """
    board = [{"slot": "Обувь", "items": [
        {"name": "Лоферы кожаные", "image": "http://img", "url": "http://buy", "brand": "Nexude"},
        {"name": "Кроссовки беговые", "image": "http://x", "url": "http://y"},
    ]}]
    cap = m._core_capsule_from_looks(LOOKS, board)
    assert "Кроссовки беговые" not in [i["name"] for i in cap]   # в образах её нет
    loafers = next(i for i in cap if "оферы" in i["name"])
    assert loafers["image"] == "http://img"
    assert loafers["image_is_example"] is True       # честно помечаем: это пример, не та самая вещь
    assert "url" not in loafers                      # к товару бренда не привязываемся
    assert "wildberries" in loafers["search"]        # ищем по описанию в любом магазине


def test_underwear_never_enters_capsule():
    cap = m._core_capsule_from_looks([{"scenario": "дом", "items": ["Пижама шёлковая", "Джемпер"]}], [])
    assert all("ижам" not in i["name"] for i in cap)
