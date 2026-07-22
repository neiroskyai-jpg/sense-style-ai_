"""Лексикон впечатления сверен с оригиналом анкеты и не расползается.

Список — калиброванный измерительный инструмент: баланс полей держит честность Identity Gap.
Слово, добавленное «вне системы», перекашивает распределение и тихо меняет метрику продукта.
Поэтому состав зафиксирован тестом, а не только договорённостью.

Опорные числа — из PDF анкеты школы «Алгоритмы имиджа» (вопросы 4 и 5), сверка 23.07.2026.
"""
from collections import Counter

import pytest

from core import lexicon as lx


# Сколько слов в каждом поле по оригиналу анкеты. Менять только вместе с первоисточником.
EXPECTED = {"natural": 15, "romance": 16, "drama": 17, "classic": 18}


def test_lexicon_matches_original_questionnaire():
    """66 слов, разложенных 15/16/17/18 — ровно как в бумажной анкете."""
    assert len(lx.LEXICON) == 66, "метод говорит «80», но по полям в анкете 66 — см. core/lexicon"
    assert Counter(lx.LEXICON.values()) == EXPECTED


def test_aliases_do_not_inflate_the_count():
    """Альтернативные написания живут отдельно и не размывают баланс полей."""
    assert set(lx.ALIASES) & set(lx.LEXICON) == set()
    assert lx.field_of("Эпатирующий") == "drama"
    assert lx.field_of("Эпатирующая") == "drama"


def test_short_quiz_set_is_balanced():
    """12 слов короткого квиза: 4 поля по 3. Перекос сместил бы Формулу и Gap."""
    assert len(lx.SHORT_SET) == 12
    assert Counter(lx.field_of(w) for w in lx.SHORT_SET) == {f: 3 for f in lx.FIELDS}


def test_short_set_words_come_from_the_lexicon():
    """Короткий квиз — выборка из анкеты, а не отдельный словарь."""
    assert all(lx.field_of(w) for w in lx.SHORT_SET)


@pytest.mark.parametrize("written,expected", [
    ("Весёлая", "natural"), ("Веселая", "natural"),      # ё и е — одно слово
    ("НАДЁЖНАЯ", "classic"), ("  Смелая  ", "drama"),    # регистр и пробелы
])
def test_word_lookup_survives_how_people_type(written, expected):
    assert lx.field_of(written) == expected


def test_distribution_sums_to_hundred():
    """Сумма ровно 100: на кривой сумме _recompute_gap отказывается считать Gap."""
    for words in (["Властная"], ["Властная", "Смелая"], ["Властная", "Смелая", "Женственная"],
                  list(lx.SHORT_SET)):
        dist = lx.distribution(words)
        assert dist is not None
        assert round(sum(dist.values()), 1) == 100.0


def test_distribution_is_none_when_nothing_recognised():
    """Пустое распределение хуже отсутствия: на нулях Gap посчитался бы «уверенно из ничего».

    Это ровно те слова, которыми короткий квиз собирал точку А до правки — они не из анкеты.
    """
    assert lx.distribution([]) is None
    assert lx.distribution(["спокойная", "незаметная", "стильная"]) is None


def test_unknown_words_are_reported_not_swallowed():
    """Слова вне лексикона видны вызывающему — замер можно пометить неполным."""
    assert lx.unknown_words(["Властная", "стильная"]) == ["стильная"]
    assert lx.unknown_words(list(lx.SHORT_SET)) == []


def test_dominant_is_reproducible_on_ties():
    """При равных долях доминанта не зависит от порядка обхода словаря."""
    tie = lx.distribution(["Властная", "Смелая"])
    assert lx.dominant(tie) == lx.dominant(dict(reversed(list(tie.items()))))
    assert lx.dominant({f: 0 for f in lx.FIELDS}) is None
