"""Стиль вещи определяется по САМОЙ вещи, а не по бренду.

Жалоба (17.07.2026): «вещи не те по стилю — романтике подсунули строгую классику». Причина: стиль
наследовался от бренда, и все 154 вещи Lichi имели «drama; romance» — юбка с воланами и костюмный
жакет были для системы одинаковы. Классическая клиентка получала кардиган в горошек и спортивную
куртку, а 14 классических жакетов того же бренда до неё не доходили: бонуса за категорию «жакет»
не было вовсе — таблица _FORMULA_CATEGORIES была заполнена только для natural.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core.catalog import Product, _FORMULA_CATEGORIES, _style_from_name, match_products  # noqa: E402


def _p(name, category, color, style_fields="", price=9999):
    return Product(id=name, name=name, brand="Lichi", category=category, price=price,
                   color=color, gender="женский", in_stock=True, style_fields=style_fields)


def test_stil_beretsya_iz_detaley_kroya():
    assert _style_from_name("Юбка макси с воланами и цветочным орнаментом") == {"romance"}
    assert _style_from_name("Приталенный двубортный жакет") == {"classic"}
    assert _style_from_name("Косуха из кожи с асимметричной молнией") == {"drama"}
    assert _style_from_name("Свободные джинсы оверсайз") == {"natural"}


def test_tkan_ne_opredelyaet_stil():
    """Шерсть/хлопок бывают в любом стиле: шерстяной жакет — классика, шерстяной оверсайз — нет."""
    assert "natural" not in _style_from_name("Приталенный жакет из шерсти")
    assert "natural" not in _style_from_name("Классическая блуза из хлопка")


def test_veshchi_odnogo_brenda_razlichayutsya():
    """Главное: у бренда один тег на весь ассортимент — вещи обязаны различаться сами."""
    jacket = _p("Приталенный однобортный жакет", "жакет", "Молочный", style_fields="drama; romance")
    skirt = _p("Юбка макси с воланами и цветочным принтом", "юбка", "Молочный", style_fields="drama; romance")
    profile = {"base_style": "classic", "styles": ["classic"], "palette": [{"name": "молочный"}],
               "stop_list": [], "gender": "женский", "price_max": 30000}
    top = match_products(profile, [skirt, jacket], k=2)
    assert top[0] is jacket, "классической клиентке жакет обязан быть выше юбки с воланами"


def test_romantike_dostayutsya_romanticheskie_veshchi():
    jacket = _p("Прямой костюмный жакет со стрелками", "жакет", "Кэмел", style_fields="classic")
    dress = _p("Платье миди из сатина с драпировкой", "платье", "Кэмел", style_fields="classic")
    profile = {"base_style": "romance", "styles": ["romance"], "palette": [{"name": "кэмел"}],
               "stop_list": [], "gender": "женский", "price_max": 30000}
    top = match_products(profile, [jacket, dress], k=2)
    assert top[0] is dress, "романтике первым должно идти платье с драпировкой, а не костюмный жакет"


def test_kategorii_zapolneny_dlya_vseh_poley():
    """Пустая таблица для classic/romance/drama лишала клиенток бонуса за нужный слот."""
    for field in ("natural", "classic", "romance", "drama"):
        assert _FORMULA_CATEGORIES.get(field), f"нет категорий-носителей для {field}"
    assert "жакет" in _FORMULA_CATEGORIES["classic"]
    assert "платье" in _FORMULA_CATEGORIES["romance"]


def test_brend_ostaetsya_folbekom():
    """~30% вещей без маркеров в названии — для них тег бренда всё ещё работает."""
    plain = _p("Жакет с карманом", "жакет", "Кремовый", style_fields="classic")
    assert _style_from_name(plain.name) == set()
    profile = {"base_style": "classic", "styles": ["classic"], "palette": [{"name": "кремовый"}],
               "stop_list": [], "gender": "женский", "price_max": 30000}
    assert match_products(profile, [plain], k=1) == [plain]
