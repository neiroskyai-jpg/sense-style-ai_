"""Метрики должны быть воспроизводимыми, а не «числом от модели».

Из ТЗ фаундера: match считается по фиксированным весам (палитра 40, силуэт и длина 30,
уместность роли 20, баланс образа 10), adds_looks — разницей комбинаторики. Жюри должно
уметь пересчитать любое число на экране руками.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

DIAG = {
    "style_formula": "Драма × Романтика × Натуральный",
    "visual_formula": {
        "silhouettes": ["полуприлегающий силуэт"],
        "palette": ["мокко", "кремовый", "хаки"],
        "stop_list": ["чисто-чёрный"],
    },
}


def _look(items, desc=""):
    return {"items": items, "description": desc, "name": ""}


def test_match_is_deterministic():
    """Одни и те же входные данные — одно и то же число. Всегда."""
    look = _look(["Жакет мокко", "Брюки кремовый", "Ботильоны", "Сумка"])
    first = m._scenario_formula_match(look, DIAG, "деловая встреча")

    assert all(m._scenario_formula_match(look, DIAG, "деловая встреча") == first for _ in range(5))


def test_breakdown_explains_the_number():
    """Explainable-слой: видно, из чего сложился процент."""
    look = _look(["Жакет мокко", "Брюки кремовый", "Ботильоны", "Сумка-багет"],
                 "полуприлегающий силуэт, драма и романтика")
    b = m._match_breakdown(look, DIAG, "деловая встреча")

    assert set(b) == {"match", "palette", "silhouette", "role", "balance", "missing"}
    recomputed = round(0.40 * b["palette"] + 0.30 * b["silhouette"]
                       + 0.20 * b["role"] + 0.10 * b["balance"])
    assert abs(recomputed - b["match"]) <= 1, (recomputed, b)


def test_stop_colour_zeroes_the_palette_axis():
    """Вещь в стоп-цвете не «частично подходит» — по палитре она не подходит вовсе."""
    bad = _look(["Платье чисто-чёрный", "Ботильоны", "Сумка", "Жакет"])

    assert m._match_breakdown(bad, DIAG, "свидание")["palette"] == 0


def test_incomplete_look_loses_balance_points():
    full = _look(["Жакет мокко", "Брюки кремовый", "Ботильоны", "Сумка-багет"])
    half = _look(["Жакет мокко", "Брюки кремовый"])

    assert m._match_breakdown(full, DIAG, "деловая встреча")["balance"] > \
        m._match_breakdown(half, DIAG, "деловая встреча")["balance"]


def test_adds_looks_counts_new_combinations():
    """«+N образов» — это комбинаторика до и после, а не оценка на глаз."""
    capsule = [{"name": "Блузка", "slot": "Верх"}, {"name": "Топ", "slot": "Верх"},
               {"name": "Брюки палаццо", "slot": "Низ"}]

    # ещё один низ работает с обоими верхами → +2
    assert m.adds_looks("Юбка миди", capsule) == 2
    # ещё один верх работает с одним низом → +1
    assert m.adds_looks("Рубашка шёлковая", capsule) == 1
    # сумка комплектов не создаёт
    assert m.adds_looks("Сумка-багет", capsule) == 0


def test_adds_looks_is_safe_without_capsule():
    """Карты ещё нет — число не выдумываем."""
    assert m.adds_looks("Юбка миди", []) == 0
    assert m.adds_looks("", [{"name": "Блузка", "slot": "Верх"}]) == 0
