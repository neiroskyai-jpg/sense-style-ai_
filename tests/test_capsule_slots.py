"""Раскладка вещей каталога по слотам гардероба — без API.

Реальный баг (2026-07-13): слот брался из `category`, а имя использовалось только при ПУСТОЙ
категории. Фиды отдают общие категории («одежда», «комплект», «трикотаж», «аксессуар»), которые
ни о чём не говорят, — и половина каталога валилась в мусорный слот «База и прочее», выдавая
жакет и косынку за «базу». Теперь имя работает как фолбэк, когда категория не распознана.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")  # импорт app.main не должен падать

from app.main import _SLOT_OTHER, _capsule_slot, _visual_capsule  # noqa: E402
from core.pipeline import _wearable_hex  # noqa: E402


def _board(n):
    """Капсула на реальном каталоге под типовой профиль. [] если каталога нет в окружении."""
    card = {"palette": [{"name": "чёрный", "hex": "#000000"}], "stop_list": []}
    diag = {"figure_type": "rectangle", "base_style": "classic",
            "semantic_field_distribution": {"classic": 60, "drama": 40}}
    return _visual_capsule(card, diag, n)


def test_verhov_bolshe_chem_nizov():
    """Канон капсулы: верхов всегда больше, чем низов — капсула богатеет за счёт верхов.
    Без квот отбор шёл по релевантности и давал 4 жакета и 3 платья на 2 верха."""
    for n in (6, 12):
        board = _board(n)
        if not board:
            return  # каталога нет в окружении
        by = {g["slot"]: len(g["items"]) for g in board}
        assert by.get("Верх", 0) > by.get("Низ", 0), f"n={n}: верхов не больше, чем низов: {by}"
        assert by.get("Верхний слой", 0) <= 2, f"n={n}: верхний слой раздут: {by}"
        assert sum(by.values()) == n, f"n={n}: собрано {sum(by.values())} вещей"


def test_v_kapsule_net_dublei_i_belya():
    """Фиды отдают один товар несколько раз («Расклешенные брюки» ×3) и подмешивают бельё
    («Подъюбник», «Топ-бра»). Ни того, ни другого в ядре капсулы быть не может."""
    board = _board(12)
    if not board:
        return
    names = [it["name"] for g in board for it in g["items"]]
    assert len(names) == len(set(names)), f"дубли в капсуле: {names}"
    assert not [n for n in names if "подъюбник" in n.lower() or "пижам" in n.lower()], names


def test_kislotnye_cveta_priglushayutsya():
    """Модель, добивая палитру до 30 цветов, скатывается в спектр — таких тканей не бывает."""
    for neon in ("#0000FF", "#FF00FF", "#00FFFF", "#FFFF00"):
        assert _wearable_hex(neon) != neon, f"{neon} остался кислотным"
    # носибельные цвета не трогаем: изумруд, бордо, королевский синий, ахроматы
    for ok in ("#0B6E4F", "#7F1734", "#2B4C9B", "#000000", "#FFFFFF"):
        assert _wearable_hex(ok) == ok, f"{ok} испорчен без нужды"


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
