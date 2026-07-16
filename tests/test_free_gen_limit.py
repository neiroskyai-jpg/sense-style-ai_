"""Текстовая Карта не должна съедать бесплатную генерацию образов.

Реальный баг (отзывы клиенток 06.07 «нет генерации образов, только текст», оценка 2, и 16.07
«не хватило визуальных примеров»): ссылка «собрать пока без образов» писала событие `card_built`,
а по нему считался лимит бесплатных генераций (FREE_GEN_LIMIT=1). В итоге клиентка сжигала
единственную бесплатную генерацию на тексте и вернуться за образами уже не могла — слово «пока»
в интерфейсе было неправдой.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from core.tracking import count_generations, record_event  # noqa: E402

EMAIL = "limit@test.ru"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Изолированная база событий на тест."""
    import core.tracking as t
    path = tmp_path / "tracking.db"
    monkeypatch.setattr(t, "DB_PATH", path)
    return path


def test_tekstovaya_karta_ne_tratit_limit(db):
    """Собрала текстовую версию → бесплатная генерация образов ОСТАЁТСЯ."""
    record_event("card_built", EMAIL, meta="text", db_path=db)
    assert count_generations(EMAIL, db_path=db) == 0, (
        "текстовая Карта съела бесплатную генерацию — клиентка не сможет получить образы"
    )


def test_karta_s_obrazami_tratit_limit(db):
    """Полная сборка (с образами) лимит тратит — защита токенов работает как раньше."""
    record_event("card_built", EMAIL, db_path=db)
    assert count_generations(EMAIL, db_path=db) == 1


def test_tekst_potom_obrazy(db):
    """Путь клиентки: текст → потом фото. Обе Карты собираются, лимит тратит только вторая."""
    record_event("card_built", EMAIL, meta="text", db_path=db)
    assert count_generations(EMAIL, db_path=db) == 0   # образы ещё доступны
    record_event("card_built", EMAIL, db_path=db)      # догенерила с фото
    assert count_generations(EMAIL, db_path=db) == 1   # теперь лимит использован


def test_chuzhaya_pochta_ne_vliyaet(db):
    record_event("card_built", "other@test.ru", db_path=db)
    assert count_generations(EMAIL, db_path=db) == 0
