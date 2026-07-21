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


def test_constructor_cells_follow_dressing_logic():
    """Порядок ячеек — как человек собирает образ, а не как устроен словарь слотов.

    Раньше ячейки шли в порядке _CAPSULE_SLOTS, и конструктор начинался с верхнего слоя:
    предлагал надеть пальто, когда под ним ещё ничего нет.
    """
    board = [{"slot": "Аксессуары"}, {"slot": "Верхний слой"}, {"slot": "Низ"},
             {"slot": "Обувь"}, {"slot": "Верх"}]

    groups = m._outfit_cells(board)

    assert [g["title"] for g in groups] == ["Основа образа", "Завершение"]
    assert groups[0]["slots"] == ["Верх", "Низ"]
    assert groups[1]["slots"] == ["Верхний слой", "Обувь", "Аксессуары"]


def test_dress_belongs_to_the_base_group():
    """Платье — самостоятельная основа образа, а не дополнение к верху и низу."""
    groups = m._outfit_cells([{"slot": "Платья и комбинезоны"}, {"slot": "Обувь"}])

    assert groups[0]["slots"] == ["Платья и комбинезоны"]


def test_cells_show_only_slots_present_in_capsule():
    """Пустых ячеек быть не должно: слот без вещей — это тупик в конструкторе."""
    groups = m._outfit_cells([{"slot": "Верх"}, {"slot": "Низ"}])

    assert [g["title"] for g in groups] == ["Основа образа"]
    assert all("Обувь" not in g["slots"] for g in groups)


def test_empty_board_gives_no_cells():
    assert m._outfit_cells([]) == []


def test_constructor_skips_items_without_photo():
    """Конструктор — визуальный инструмент: вещь без картинки в нём бесполезна.

    На проде половина плиток была пустыми бежевыми прямоугольниками — вещи капсулы, которым
    не нашлось фото. Перетащить такую в образ и увидеть результат нельзя.
    """
    own = [{"slot": "Верх", "items": [{"name": "Блузка", "image": "data:x"},
                                      {"name": "Топ без фото"}]}]
    extra = [{"slot": "Низ", "items": [{"name": "Брюки", "image": "data:y"}]}]

    board = m._merge_boards(own, extra, limit=10)
    names = [it["name"] for grp in board for it in grp["items"]]

    assert "Топ без фото" not in names
    assert names == ["Блузка", "Брюки"]
