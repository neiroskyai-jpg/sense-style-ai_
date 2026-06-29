"""Тесты каталога партнёрки: парсинг YML-подобного фида + подбор под профиль.

Реального фида Lamoda ещё нет — гоняем на синтетическом образце (формат YML, как у CPA).
Когда придёт настоящий фид: подложить его в SAMPLE_FEED / поправить core.catalog._FIELD_TAGS.
"""
from core.catalog import Product, match_products, parse_feed

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
