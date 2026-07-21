"""Экономика капсулы: ответ на «дорого», посчитанный кодом.

Из ТЗ фаундера (пункт 7): «7 вещей = 21 образ», cost-per-wear, «не купила N лишних вещей».
Требование там же — все числа считаются на бэкенде и воспроизводимы: жюри должно уметь
пересчитать любое на бумаге.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

CAPSULE = [
    {"name": "Блузка шёлковая", "slot": "Верх", "price": 6000},
    {"name": "Топ из вискозы", "slot": "Верх", "price": 4000},
    {"name": "Брюки палаццо", "slot": "Низ", "price": 8000},
    {"name": "Юбка миди", "slot": "Низ", "price": 6000},
]


def test_cost_per_look_is_plain_arithmetic():
    """Вся капсула ÷ число образов. Проверяется на бумаге."""
    e = m.capsule_economics(CAPSULE)

    assert e["looks"] == 4                      # 2 верха × 2 низа
    assert e["total"] == 24000
    assert e["cost_per_look"] == 6000           # 24000 / 4


def test_saved_items_compares_with_standalone_outfits():
    """«Не купила N вещей» = сколько ушло бы на столько же отдельных комплектов минус капсула."""
    e = m.capsule_economics(CAPSULE)

    assert e["standalone_items"] == 12          # 4 образа × 3 вещи
    assert e["saved_items"] == 8                # 12 − 4 вещи капсулы


def test_no_inflated_money_saving_is_reported():
    """Денежную «экономию» не показываем: на реальной капсуле выходило больше миллиона рублей.

    Арифметика верна, но стоит на двойном допущении — что клиентка купила бы все эти вещи и по
    той же средней цене. Такое число рассыпается от первого вопроса и подрывает остальные.
    """
    e = m.capsule_economics(CAPSULE)

    assert "cost_without_capsule" not in e
    assert "saved_money" not in e


def test_missing_prices_hide_money_metrics_only():
    """Без цен денежные числа не выдумываем, но «сколько вещей не купили» остаётся."""
    e = m.capsule_economics([{"name": n, "slot": s} for n, s in
                             [("Блузка", "Верх"), ("Топ", "Верх"), ("Брюки", "Низ")]])

    assert e["has_prices"] is False
    assert e["cost_per_look"] == 0
    assert e["saved_items"] > 0


def test_empty_capsule_returns_nothing():
    """Выдуманное число хуже отсутствующего."""
    assert m.capsule_economics([]) is None
    assert m.capsule_economics(None) is None


def test_capsule_without_combinations_is_not_divided_by_zero():
    """Одни аксессуары комплектов не образуют — делить не на что."""
    assert m.capsule_economics([{"name": "Сумка", "slot": "Аксессуары", "price": 5000}]) is None


def test_card_shows_the_block():
    assert "class=econ" in m.STYLE_CARD
    assert "стоит один собранный образ" in m.STYLE_CARD


def test_price_hidden_when_known_for_minority():
    """На проде цена нашлась у одной вещи из девяти, и клиентка увидела «378 ₽ за образ».

    Сумма делилась на все образы — арифметика верная, смысл абсурдный. Неполные данные хуже
    отсутствующих: показываем цену, только если она известна у большинства вещей капсулы.
    """
    thin = [{"name": "A", "slot": "Верх", "price": 6799},
            {"name": "B", "slot": "Верх"},
            {"name": "C", "slot": "Низ"},
            {"name": "D", "slot": "Низ"}]

    e = m.capsule_economics(thin)

    assert e["has_prices"] is False
    assert e["cost_per_look"] == 0
    assert e["saved_items"] > 0, "метрика вещей от цен не зависит и остаётся"


def test_price_shown_when_known_for_most():
    full = [{"name": "A", "slot": "Верх", "price": 6000},
            {"name": "B", "slot": "Верх", "price": 4000},
            {"name": "C", "slot": "Низ", "price": 8000}]

    assert m.capsule_economics(full)["cost_per_look"] == 9000


def test_extras_survive_legacy_capsule_format():
    """Данные в базе переживают деплой: старые Карты хранят капсулу списком СТРОК.

    Из-за этого Карта на проде отдавала Internal Server Error — блок-новичок ронял весь
    продукт. Надстройки над капсулой обязаны переживать любой формат.
    """
    legacy = ["Жакет чёрный", "Брюки прямые"]
    mixed = ["Жакет", {"name": "Брюки", "slot": "Низ"}, None]

    for capsule in (legacy, mixed):
        m.build_outfit_matrix(capsule)      # не должно падать
        m.capsule_economics(capsule)


def test_broken_extra_never_takes_down_the_card():
    """Лучше Карта без одного блока, чем 500 вместо Карты."""
    def boom(*_a):
        raise ValueError("сломался блок")

    assert m._safe_extra(boom, []) is None
