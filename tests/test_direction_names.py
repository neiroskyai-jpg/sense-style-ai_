"""Названия направлений — человеческие, без нашего внутреннего жаргона.

Жалоба фаундера по скриншоту с прода: клиентке показали «Спокойный регистр» и «Уверенный
регистр». «Регистр» — термин методологии; на экране он читается как техническая пометка,
а не как название её образа. Слово попало в клиентский текст прямо из промпта, где стояло
в примерах допустимых названий.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402

DIAG = {"style_formula": "Классика × Натуральность", "primary_substyle": "Чистая классика"}


def test_jargon_is_not_in_the_prompt_examples():
    """Модель повторяет примеры из промпта — значит примеров с «регистром» там быть не должно.

    Смотрим именно перечень допустимых названий, до блока с запретом: ниже слово «регистр»
    встречается законно, в объяснении, почему его нельзя показывать.
    """
    allowed = p._DIRECTIONS_SYSTEM.split("СЛОВО «РЕГИСТР»")[0]

    assert "СЛОВО «РЕГИСТР» КЛИЕНТКЕ НЕ ПОКАЗЫВАЕМ" in p._DIRECTIONS_SYSTEM, "запрет на месте"
    assert "регистр" not in allowed.lower(), "в примерах допустимых названий жаргона быть не должно"


def test_register_in_name_is_repaired_keeping_the_meaning():
    """«Спокойный регистр» → «Спокойный вариант»: характер направления сохраняем, термин убираем."""
    out = p._canonical_direction_names(
        [{"name": "Спокойный регистр"}, {"name": "Уверенный регистр"}], DIAG)

    assert out[0]["name"] == "Спокойный вариант"
    assert out[1]["name"] == "Уверенный вариант"


def test_other_jargon_is_caught_too():
    for bad in ("Семантический профиль", "Дескриптор образа"):
        name = p._canonical_direction_names([{"name": bad}], DIAG)[0]["name"]
        assert "регистр" not in name.lower()
        assert bad.lower() not in name.lower()


def test_human_names_are_left_alone():
    """Нормальные названия не трогаем — иначе вычистим и то, что писать можно."""
    for good in ("Мягкая версия", "Тихое прочтение", "Собранная версия"):
        assert p._canonical_direction_names([{"name": good}], DIAG)[0]["name"] == good


def test_client_own_substyle_survives():
    """Название из её собственной Формулы — не выдумка и не жаргон, оставляем как есть."""
    out = p._canonical_direction_names([{"name": "Чистая классика"}], DIAG)

    assert out[0]["name"] == "Чистая классика"
