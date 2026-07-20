"""Тесты каталога партнёрки: парсинг YML-подобного фида + подбор под профиль.

Реального фида Lamoda ещё нет — гоняем на синтетическом образце (формат YML, как у CPA).
Когда придёт настоящий фид: подложить его в SAMPLE_FEED / поправить core.catalog._FIELD_TAGS.
"""
from core.catalog import (Product, _color_families, match_products, parse_csv,
                          parse_feed, products_to_csv, score_products)

SAMPLE_FEED = """<?xml version="1.0" encoding="utf-8"?>
<yml_catalog>
  <shop>
    <offers>
      <offer id="1001" available="true">
        <name>Жакет из мягкой шерсти</name>
        <vendor>Lamoda Brand</vendor>
        <price>7990</price>
        <oldprice>9990</oldprice>
        <currencyId>RUB</currencyId>
        <url>https://lamoda.ru/p/1001?partner=cpa</url>
        <picture>https://img/1001.jpg</picture>
        <param name="категория">жакет</param>
        <param name="цвет">бежевый</param>
        <param name="пол">женский</param>
        <param name="размер">42,44,46</param>
      </offer>
      <offer id="1002" available="true">
        <name>Брюки прямые льняные</name>
        <vendor>Lamoda Brand</vendor>
        <price>4500</price>
        <url>https://lamoda.ru/p/1002?partner=cpa</url>
        <picture>https://img/1002.jpg</picture>
        <param name="категория">брюки</param>
        <param name="цвет">коричневый</param>
        <param name="пол">женский</param>
      </offer>
      <offer id="1003" available="false">
        <name>Платье-футляр</name>
        <price>6000</price>
        <param name="категория">платье</param>
        <param name="цвет">чёрный</param>
        <param name="пол">женский</param>
      </offer>
      <offer id="1004" available="true">
        <name>Рубашка мужская</name>
        <price>3000</price>
        <param name="категория">рубашка</param>
        <param name="цвет">синий</param>
        <param name="пол">мужской</param>
      </offer>
    </offers>
  </shop>
</yml_catalog>"""


def test_parse_basic_fields():
    products = parse_feed(SAMPLE_FEED)
    assert len(products) == 4
    jacket = next(p for p in products if p.id == "1001")
    assert jacket.name == "Жакет из мягкой шерсти"
    assert jacket.price == 7990.0
    assert jacket.old_price == 9990.0
    assert jacket.color == "бежевый"
    assert jacket.gender == "женский"
    assert jacket.sizes == ["42", "44", "46"]
    assert jacket.url.endswith("partner=cpa")


def test_availability_flag():
    products = {p.id: p for p in parse_feed(SAMPLE_FEED)}
    assert products["1001"].in_stock is True
    assert products["1003"].in_stock is False


def test_match_filters_gender_stock_and_palette():
    profile = {
        "figure_type": "rectangle",
        "base_style": "natural",
        "palette": [{"name": "бежевый"}, {"name": "коричневый"}],
        "stop_list": ["чёрный"],
        "price_max": 10000,
        "gender": "female",
    }
    picks = match_products(profile, parse_feed(SAMPLE_FEED), k=12)
    ids = [p.id for p in picks]

    assert "1004" not in ids           # мужская — отсечена
    assert "1003" not in ids           # нет в наличии + чёрный (стоп) + футляр (антипаттерн)
    assert ids[:2] == ["1001", "1002"] or set(ids) == {"1001", "1002"}  # бежевый/коричневый, женские, в наличии


def test_empty_and_garbage_safe():
    assert match_products({}, [], k=5) == []
    p = Product(id="x", name="без полей")
    assert match_products({"base_style": "natural"}, [p], k=5) == [p] or isinstance(
        match_products({"base_style": "natural"}, [p], k=5), list)


def test_color_families_map_method_names_to_base():
    # имена палитры метода и сырые цвета фида сходятся к одной семье
    assert _color_families("Чёрная ночь") == {"black"}
    assert "grey" in _color_families("Холодный тауп")
    assert _color_families("Рубиновый") == {"red"}
    assert _color_families("чёрный") == {"black"}


def test_match_by_palette_family_not_substring():
    # раньше «Чёрная ночь» не находила «чёрный» (подстрока), теперь находит по семье
    prods = [
        Product(id="1", name="Пиджак", category="пиджак", color="чёрный", gender="женский"),
        Product(id="2", name="Юбка", category="юбка", color="тауп", gender="женский"),
        Product(id="3", name="Топ", category="топ", color="жёлтый", gender="женский"),
    ]
    profile = {"palette": [{"name": "Чёрная ночь"}, {"name": "Холодный тауп"}],
               "stop_list": ["Горчичный"], "gender": "женский"}
    picks = match_products(profile, prods, k=12)
    ids = [p.id for p in picks]
    assert "3" not in ids            # жёлтый в стоп-семье (горчичный) — отсечён
    assert ids[:2] == ["1", "2"]     # чёрный и тауп из палитры — выше жёлтого вне палитры


def test_match_prefers_client_style_fields():
    # доминанта клиентки — classic; вещь classic-бренда выше drama-бренда при равном цвете
    prods = [
        Product(id="c", name="Жакет", category="жакет", color="чёрный",
                gender="женский", style_fields="classic; natural"),
        Product(id="d", name="Платье", category="платье", color="чёрный",
                gender="женский", style_fields="drama; romance"),
    ]
    profile = {"palette": [{"name": "Чёрная ночь"}], "styles": ["classic"], "gender": "женский"}
    picks = match_products(profile, prods, k=12)
    assert picks[0].id == "c"  # classic-вещь впереди drama при равном цвете


def test_soft_colortype_penalizes_black_outside_palette():
    prods = [
        Product(id="b", name="Жакет классический", category="жакет", color="чёрный", gender="женский"),
        Product(id="m", name="Жакет классический", category="жакет", color="молочный", gender="женский"),
    ]
    profile = {
        "palette": [{"name": "молочный"}],
        "colortype": "summer_natural",
        "base_style": "classic",
        "gender": "женский",
    }

    picks = match_products(profile, prods, k=12)

    assert picks[0].id == "m"


def test_figure_preference_promotes_better_bottom_model():
    prods = [
        Product(id="wide", name="Джинсы wide leg", category="джинсы", color="синий", gender="женский"),
        Product(id="straight", name="Джинсы straight", category="джинсы", color="синий", gender="женский"),
    ]
    profile = {
        "palette": [{"name": "синий"}],
        "figure_type": "rectangle",
        "gender": "женский",
    }

    scored = score_products(profile, prods)

    assert [p.id for _, p in scored][:2] == ["wide", "straight"]


def test_parse_csv_and_roundtrip(tmp_path):
    """CSV (выгрузка скрейпера) → Product → match_products; и обратная запись."""
    src = tmp_path / "prods.csv"
    src.write_text(
        "id,name,brand,category,price,color,url,gender,sizes\n"
        "80877,Топ-пиджак из сатина,Ushatava,пиджак,49900,чёрный,https://u.ru/1,женский,\"S,M\"\n"
        "80900,Брюки прямые,Ushatava,брюки,22900,тауп,https://u.ru/2,женский,M\n",
        encoding="utf-8-sig",
    )
    prods = parse_csv(src)
    assert len(prods) == 2
    assert prods[0].brand == "Ushatava" and prods[0].price == 49900.0
    assert prods[0].sizes == ["S", "M"]
    # идёт в тот же движок подбора
    top = match_products({"base_style": "natural", "palette": [{"name": "тауп"}],
                          "gender": "женский"}, prods, k=2)
    assert any(p.color == "тауп" for p in top)
    # round-trip: запись → чтение не теряет полей
    dest = tmp_path / "out.csv"
    products_to_csv(prods, dest)
    assert len(parse_csv(dest)) == 2
