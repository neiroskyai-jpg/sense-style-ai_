"""Личный гардероб: «брать / не брать» должно иметь последствие.

Фаундер: «если человек сам хочет купить, там брать не брать, и эта вещь к нему добавляется».
Раньше проверка давала вердикт и на этом заканчивалась — вещь никуда не сохранялась.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402
from core.profiles import add_wardrobe_item, delete_wardrobe_item, wardrobe_items  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "profiles.db"
    monkeypatch.setattr("core.profiles.DB_PATH", path)
    return path


def test_item_is_saved_and_listed(db):
    add_wardrobe_item("anon-1", {"name": "Тренч бежевый", "slot": "Верхний слой",
                                 "verdict": "Брать", "reason": "в палитре"}, db_path=db)
    items = wardrobe_items("anon-1", db_path=db)
    assert [i["name"] for i in items] == ["Тренч бежевый"]
    assert items[0]["slot"] == "Верхний слой"


def test_newest_first(db):
    add_wardrobe_item("anon-1", {"name": "Первая"}, db_path=db)
    add_wardrobe_item("anon-1", {"name": "Вторая"}, db_path=db)
    assert [i["name"] for i in wardrobe_items("anon-1", db_path=db)] == ["Вторая", "Первая"]


def test_wardrobe_is_per_user(db):
    """Чужие вещи не видны — гардероб личный."""
    add_wardrobe_item("anon-1", {"name": "Её тренч"}, db_path=db)
    assert wardrobe_items("anon-2", db_path=db) == []


def test_remove(db):
    add_wardrobe_item("anon-1", {"name": "Лоферы"}, db_path=db)
    item_id = wardrobe_items("anon-1", db_path=db)[0]["id"]
    delete_wardrobe_item("anon-1", item_id, db_path=db)
    assert wardrobe_items("anon-1", db_path=db) == []


def test_nameless_item_ignored(db):
    """Без названия сохранять нечего — не плодим пустые карточки."""
    add_wardrobe_item("anon-1", {"slot": "Обувь"}, db_path=db)
    assert wardrobe_items("anon-1", db_path=db) == []


def test_slot_is_derived_from_name():
    """Слот определяется по названию — вещь встаёт в свою полку гардероба."""
    assert m._capsule_slot("Тренч бежевый") == "Верхний слой"
    assert m._capsule_slot("Лоферы кожаные") == "Обувь"


def test_merge_moves_everything_to_email(db):
    """Почта после квиза не должна стоить клиентке результата.

    Она проходит путь анонимно, потом оставляет почту — как мы и просим. Без переноса Карта,
    замеры и гардероб остаются под старым anon-id, и человек теряет ровно то, что собрал.
    """
    from core.profiles import (card_versions, get_profile, merge_profile, save_card,
                               save_diagnosis)
    save_diagnosis("anon-x", {"style_formula": "Классика"}, db_path=db)
    save_card("anon-x", {"formula": "Классика", "season": "fw"}, db_path=db)
    add_wardrobe_item("anon-x", {"name": "Тренч"}, db_path=db)

    assert merge_profile("anon-x", "anna@example.com", db_path=db) is True

    prof = get_profile("anna@example.com", db_path=db)
    assert prof["card"]["formula"] == "Классика"
    assert prof["diagnosis"]["style_formula"] == "Классика"
    assert [i["name"] for i in wardrobe_items("anna@example.com", db_path=db)] == ["Тренч"]
    assert card_versions("anna@example.com", db_path=db)
    assert get_profile("anon-x", db_path=db) == {}


def test_merge_does_not_overwrite_existing_account(db):
    """Аккаунт старше анонимной сессии — его Карту не затираем."""
    from core.profiles import get_profile, merge_profile, save_card
    save_card("anna@example.com", {"formula": "Своя Карта"}, db_path=db)
    save_card("anon-y", {"formula": "Анонимная"}, db_path=db)
    merge_profile("anon-y", "anna@example.com", db_path=db)
    assert get_profile("anna@example.com", db_path=db)["card"]["formula"] == "Своя Карта"
