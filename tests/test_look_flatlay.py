"""Раскладка образа: он на клиентке + вещи, из которых собран (flat-lay).

Идея фаундера: связать образ с капсулой визуально — рядом с образом показать фото каждой вещи,
как раскладка в модном разборе. Текст «Состав: жакет · брюки» этого не давал: клиентка не видела
вещи и не понимала, что образ собран из её же капсулы.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

BOARD = [
    {"slot": "Верхний слой", "items": [{"name": "Приталенный пиджак", "image": "data:a"}]},
    {"slot": "Верх", "items": [{"name": "Блузка шёлковая", "image": "data:b"}]},
    {"slot": "Низ", "items": [{"name": "Брюки палаццо мокко", "image": "data:c"}]},
    {"slot": "Обувь", "items": [{"name": "Лоферы кожаные", "image": "data:d"}]},
]


def test_pieces_get_photos_from_capsule():
    pieces = m._look_pieces(["Приталенный пиджак", "Брюки палаццо мокко", "Лоферы кожаные"], BOARD)

    assert len(pieces) == 3
    assert all(p["image"] for p in pieces), "каждая вещь образа должна получить фото"


def test_piece_without_match_falls_back_to_slot():
    """Точного совпадения имени нет — берём вещь того же слота с фото (иллюстрация типа)."""
    pieces = m._look_pieces(["Жакет оливковый структурный"], BOARD)

    assert pieces[0]["slot"] == "Верхний слой"
    assert pieces[0]["image"] == "data:a"
    assert pieces[0]["image_is_example"] is True


def test_photo_is_marked_as_example():
    """Фото — иллюстрация типа вещи, не конкретный товар: продукт не привязан к фиду бренда."""
    pieces = m._look_pieces(["Брюки палаццо мокко"], BOARD)

    assert pieces[0]["image_is_example"] is True


def test_skeleton_looks_carry_pieces(monkeypatch):
    """Без генерации фото на клиентке раскладка вещей всё равно собирается из капсулы.

    Это то, что видит жюри, проходя квиз без своего фото: образ = реальные вещи капсулы.

    Капсулу подаём фиксированную. Раньше тест опирался на живой каталог и падал через раз:
    если подбор возвращал вещи не тех слотов, раскладка законно оказывалась пустой — тест
    проверял состояние каталога, а не логику, которую должен стеречь.
    """
    board = [
        {"slot": "Верх", "items": [{"name": "Блузка шёлковая", "image": "data:x"}]},
        {"slot": "Низ", "items": [{"name": "Брюки палаццо", "image": "data:y"}]},
        {"slot": "Обувь", "items": [{"name": "Ботильоны", "image": "data:z"}]},
        {"slot": "Аксессуары", "items": [{"name": "Сумка-тоут", "image": "data:w"}]},
    ]
    monkeypatch.setattr(m, "_visual_capsule", lambda *a, **k: board)
    monkeypatch.setattr(m, "_inline_capsule_images", lambda b: b)
    diag = {
        "style_formula": "Классика × Натуральность", "gap_percentage": 38,
        "colortype": "autumn_natural", "figure_type": "hourglass",
        "visual_formula": {"silhouettes": ["Полуприлегающий"],
                           "palette": ["Тёплый бежевый", "Графит"], "stop_list": []},
    }
    card = m.build_card_skeleton(diag, season="autumn")

    with_pieces = [lk for lk in card["looks"] if lk.get("pieces")]
    assert with_pieces, "у скелетных образов должна быть раскладка из капсулы"
    assert any(pc.get("image") for lk in with_pieces for pc in lk["pieces"]),         "раскладка обязана нести фото вещей, иначе это снова текстовый список"
    first = with_pieces[0]
    assert all(p["image"] for p in first["pieces"]), "вещи капсулы идут с фото"


def test_template_renders_flatlay():
    assert "class=lookflat" in m.STYLE_CARD or 'class="lookflat' in m.STYLE_CARD
    assert "lookpiece" in m.STYLE_CARD


def test_photo_matches_the_item_kind_not_just_the_slot():
    """Слот слишком широк: «Лодочки» и «Угги» оба Обувь.

    Реальный провал на проде: под названием «Лодочки на устойчивом каблуке» стояло фото угг,
    под «Сумка структурированная» — рекламный коллаж «СУМКА ШОППЕР». Клиентка видела правильные
    названия и чужие картинки, и вся раскладка читалась как случайные вещи из интернета.
    """
    board = [{"slot": "Обувь", "items": [
        {"name": "Угги женские зимние", "image": "UGG"},
        {"name": "Лодочки кожаные", "image": "PUMPS"}]}]

    pieces = m._look_pieces(["Лодочки на устойчивом каблуке"], board)

    assert pieces[0]["image"] == "PUMPS", "под лодочки нельзя подставлять угги"


def test_missing_kind_leaves_empty_card():
    """Чужое фото под правильным названием хуже отсутствующего: клиентка видит вещь,
    которой в её образе нет."""
    board = [{"slot": "Аксессуары", "items": [{"name": "Сумка-тоут", "image": "BAG"}]}]

    pieces = m._look_pieces(["Шарф шёлковый"], board)

    assert pieces[0]["image"] is None
    assert pieces[0]["name"]


def test_packshot_wins_over_marketplace_collage():
    """У маркетплейсных кадров «на модели» часто рекламный коллаж с текстом поверх картинки."""
    board = [{"slot": "Аксессуары", "items": [
        {"name": "Сумка шоппер мешок", "image": "COLLAGE", "image_kind": "model"},
        {"name": "Сумка структурная", "image": "CLEAN", "image_kind": "packshot"}]}]

    pieces = m._look_pieces(["Сумка структурированная"], board)

    assert pieces[0]["image"] == "CLEAN"


def test_clean_brand_sources_win_over_marketplace():
    """На фото маркетплейса часто нарисован рекламный текст поверх вещи («ТРЕНД 2026», логотип
    магазина). По типу изображения это честный packshot — распознать его нельзя, поэтому
    решаем источником: брендовая студийная съёмка идёт вперёд.
    """
    assert m._CLEAN_SOURCES, "список чистых источников не должен быть пустым"

    class P:
        def __init__(self, brand): self.brand = brand

    assert m._is_clean_source(P("Lichi"))
    assert m._is_clean_source(P("USHATAVA"))
    assert not m._is_clean_source(P("CAMILLE-OSENSA"))
    assert not m._is_clean_source(P(""))
