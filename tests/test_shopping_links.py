"""Тест генерации deep-link маркетплейсов (без API)."""
from core.pipeline import marketplace_links


def test_marketplace_links_encode_query():
    links = marketplace_links("жакет шерсть графит")
    assert links["wildberries"].startswith("https://www.wildberries.ru/")
    assert links["lamoda"].startswith("https://www.lamoda.ru/")
    assert links["ozon"].startswith("https://www.ozon.ru/")
    # пробелы и кириллица закодированы (нет сырых пробелов в URL)
    assert " " not in links["wildberries"]
    assert "%" in links["lamoda"]
