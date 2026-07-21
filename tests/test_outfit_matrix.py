"""Матрица «база × слой»: капсула как гардероб, а не как список покупок.

Формат, который у конкурентов собирает больше всего сохранений, и ответ на жалобу «капсула
отдельно, образы отдельно»: видно, что одни и те же вещи дают разные образы под разные роли.
Считается кодом из существующей капсулы — ни одной генерации.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

CAPSULE = [
    {"name": "Блузка шёлковая", "slot": "Верх"},
    {"name": "Топ из вискозы", "slot": "Верх"},
    {"name": "Брюки палаццо", "slot": "Низ"},
    {"name": "Жакет структурный", "slot": "Верхний слой"},
    {"name": "Ботильоны", "slot": "Обувь"},
    {"name": "Сумка-тоут", "slot": "Аксессуары"},
]


def test_matrix_multiplies_bases_by_layers():
    """Смысл матрицы: 2 базы × 2 колонки (без слоя и со слоем) = 4 образа."""
    mx = m.build_outfit_matrix(CAPSULE)

    assert mx["columns"] == ["Без слоя", "Жакет структурный"]
    assert len(mx["rows"]) == 2
    assert mx["total"] == 4


def test_same_base_gets_different_roles():
    """Одна база в разных ролях — это и есть «мало вещей, много образов»."""
    row = m.build_outfit_matrix(CAPSULE)["rows"][0]
    roles = [c["role"] for c in row["cells"]]

    assert roles == ["Повседневное", "Работа"]
    assert all(c["why"] for c in row["cells"]), "роль без объяснения — просто ярлык"


def test_dress_is_a_base_on_its_own():
    """Платье — готовый образ, а не дополнение к верху и низу."""
    mx = m.build_outfit_matrix(CAPSULE + [{"name": "Платье миди", "slot": "Платья и комбинезоны"}])
    dress_row = [r for r in mx["rows"] if r["kind"] == "dress"]

    assert dress_row, "платье обязано быть отдельной базой"
    assert [c["role"] for c in dress_row[0]["cells"]] == ["Свидание", "Выход"]


def test_shoes_and_bag_are_taken_as_a_pair():
    """Правило метода: обувь и сумка держат образ и подбираются в тон друг другу, значит
    берутся одной парой по одному индексу, а не двумя независимыми."""
    capsule = CAPSULE + [{"name": "Лодочки", "slot": "Обувь"},
                         {"name": "Клатч", "slot": "Аксессуары"}]
    mx = m.build_outfit_matrix(capsule)
    cell = mx["rows"][0]["cells"][0]
    shoes = [i for i in cell["items"] if i in ("Ботильоны", "Лодочки")]
    bags = [i for i in cell["items"] if i in ("Сумка-тоут", "Клатч")]

    assert len(shoes) == 1 and len(bags) == 1
    # один индекс на пару: первая обувь идёт с первой сумкой
    assert (shoes[0] == "Ботильоны") == (bags[0] == "Сумка-тоут")


def test_no_bases_means_no_matrix():
    """Из одних аксессуаров образ не собрать — выдуманную матрицу не показываем."""
    assert m.build_outfit_matrix([{"name": "Сумка", "slot": "Аксессуары"}]) is None
    assert m.build_outfit_matrix([]) is None


def test_key_does_not_collide_with_dict_methods():
    """`matrix.items` в Jinja резолвится в метод словаря: на экране был
    «<built-in method items of dict>» вместо числа вещей."""
    mx = m.build_outfit_matrix(CAPSULE)

    assert "items" not in mx, "имя ключа не должно совпадать с методом dict"
    assert mx["items_count"] == len(CAPSULE)


def test_card_renders_the_matrix():
    assert "class=matrix" in m.STYLE_CARD
    assert "matrix.items_count" in m.STYLE_CARD
