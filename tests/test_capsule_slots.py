"""Раскладка вещей каталога по слотам гардероба — без API.

Реальный баг (2026-07-13): слот брался из `category`, а имя использовалось только при ПУСТОЙ
категории. Фиды отдают общие категории («одежда», «комплект», «трикотаж», «аксессуар»), которые
ни о чём не говорят, — и половина каталога валилась в мусорный слот «База и прочее», выдавая
жакет и косынку за «базу». Теперь имя работает как фолбэк, когда категория не распознана.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")  # импорт app.main не должен падать

from app.main import _SLOT_OTHER, _capsule_slot  # noqa: E402


def test_obshaya_kategoriya_reshaetsya_po_imeni():
    """Категория общая → слот определяется по имени, а не «Прочее»."""
    assert _capsule_slot("одежда", "Топ из вискозы") == "Верх"
    assert _capsule_slot("одежда", "Расклешенные брюки") == "Низ"
    assert _capsule_slot("комплект", "Однобортный жакет") == "Верхний слой"
    assert _capsule_slot("аксессуар", "Косынка из шёлка") == "Аксессуары"
    assert _capsule_slot("трикотаж", "Джемпер из шерсти") == "Верх"


def test_tochnaya_kategoriya_vyigryvaet():
    """Если категория распознана — она и решает, имя не нужно."""
    assert _capsule_slot("платье", "") == "Платья и комбинезоны"
    assert _capsule_slot("пиджак", "Пиджак из органзы") == "Верхний слой"
    assert _capsule_slot("юбка", "Юбка миди") == "Низ"


def test_nerazpoznannoe_padaet_v_prochee():
    """Ни категория, ни имя не читаются → честное «Прочее» (не «База»)."""
    assert _capsule_slot("", "") == _SLOT_OTHER
    assert _capsule_slot("хтоническое", "нечто безымянное") == _SLOT_OTHER
    assert _SLOT_OTHER == "Прочее", "мусорный слот не должен называться «базой»"


def test_realnyy_katalog_ne_svalivaetsya_v_prochee():
    """Боевой фид (Lichi + Ushatava) раскладывается по слотам, мусорный слот пуст."""
    import csv
    import pathlib

    rows = []
    for name in ("products_lichi.csv", "products_ushatava.csv"):
        path = pathlib.Path("data/fashion-base") / name
        if path.exists():
            rows += list(csv.DictReader(path.open(encoding="utf-8-sig")))
    if not rows:
        return  # каталога нет в окружении — тест неприменим

    slots = [_capsule_slot(r.get("category"), r.get("name")) for r in rows]
    prochee = [s for s in slots if s == _SLOT_OTHER]
    assert not prochee, f"{len(prochee)} вещей упало в «Прочее» — классификация слотов сломана"
    assert len(set(slots)) >= 4, "капсула из одного-двух слотов — это не гардероб"
