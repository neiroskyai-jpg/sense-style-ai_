"""Капсула эволюционирует между сезонами — и это видно.

Переключение сезона и раньше пересобирало капсулу, но клиентка видела просто другой набор вещей:
что ушло, почему и что это дало — не показывалось. Капсула выглядела случайной, а не живой.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

SUMMER = [
    {"name": "Лёгкий плащ хаки", "slot": "Верхний слой"},
    {"name": "Босоножки на каблуке", "slot": "Обувь"},
    {"name": "Блузка шёлковая", "slot": "Верх"},
    {"name": "Брюки палаццо мокко", "slot": "Низ"},
]
WINTER = [
    {"name": "Пальто структурное серое", "slot": "Верхний слой"},
    {"name": "Ботильоны кожаные", "slot": "Обувь"},
    {"name": "Блузка шёлковая", "slot": "Верх"},
    {"name": "Водолазка кашемир", "slot": "Верх"},
    {"name": "Брюки палаццо мокко", "slot": "Низ"},
]


def test_diff_lists_what_left_and_what_came():
    d = m.capsule_diff(SUMMER, WINTER, "winter")

    left = {r["name"] for r in d["removed"]}
    came = {a["name"] for a in d["added"]}

    assert left == {"Лёгкий плащ хаки", "Босоножки на каблуке"}
    assert came == {"Пальто структурное серое", "Ботильоны кожаные", "Водолазка кашемир"}
    assert d["kept_count"] == 2, "вещи вне сезона не трогаем — капсула не собирается заново"


def test_seasonal_reason_is_specific():
    """«Не по сезону» без объяснения выглядит как произвол алгоритма."""
    d = m.capsule_diff(SUMMER, WINTER, "winter")

    for r in d["removed"]:
        assert "не по зиме" in r["why"], r


def test_diff_counts_combinations():
    """Главное обещание метода — «мало вещей, много образов». Переход обязан это показывать."""
    d = m.capsule_diff(SUMMER, WINTER, "winter")

    assert d["combinations_after"] > d["combinations_before"]
    assert d["combinations_delta"] == d["combinations_after"] - d["combinations_before"]


def test_added_items_are_ranked_by_contribution():
    """Сверху то, что даёт больше сочетаний, — это и есть ценность замены."""
    d = m.capsule_diff(SUMMER, WINTER, "winter")
    contributions = [a["adds_looks"] for a in d["added"]]

    assert contributions == sorted(contributions, reverse=True)


def test_identical_capsules_report_no_change():
    """Ничего не изменилось — блок показывать не надо."""
    d = m.capsule_diff(SUMMER, list(SUMMER), "summer")

    assert d["changed"] is False
    assert d["removed"] == [] and d["added"] == []


def test_cabinet_template_renders_the_block():
    assert "Капсула пересобрана:" in m.CABINET_PAGE
    assert "Ушли из капсулы" in m.CABINET_PAGE and "Пришли на замену" in m.CABINET_PAGE
