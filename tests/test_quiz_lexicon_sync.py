"""Слова квиза не расходятся с лексиконом метода.

Список из 12 слов физически лежит в двух местах: `core/lexicon.SHORT_SET` (сервер считает по нему
распределение) и в разметке `web/identity-scan-quiz.html` (клиентка ими отвечает). Дублирование
здесь осознанное — квиз это статический файл без шаблонизатора, — но расхождение тихо ломает
Identity Gap: слово, которого нет в лексиконе, роняет замер в клиентскую заглушку, и метрика
снова становится константой. Поэтому совпадение проверяется тестом, а не договорённостью.

Обе точки (q14 «сейчас» и q13 «хочу») обязаны использовать ОДИН список — как вопросы 4 и 5
бумажной анкеты школы. Разные линейки для двух точек делают разрыв между ними бессмысленным.
"""
import re
from collections import Counter
from pathlib import Path

import pytest

from core import lexicon as lx

QUIZ = Path(__file__).resolve().parent.parent / "web" / "identity-scan-quiz.html"


def _multi_questions() -> dict[str, list[str]]:
    """{роль: [слова]} для всех multi-вопросов квиза."""
    src = QUIZ.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for block in re.findall(r"\{\s*id:\s*\d+,\s*block:.*?\n\s*\},", src, re.S):
        if 'kind: "multi"' not in block:
            continue
        role = re.search(r'role:\s*"(\w+)"', block)
        words = re.findall(r'\{\s*t:\s*"([^"]+)"', block)
        out[role.group(1) if role else "без роли"] = words
    return out


def test_both_points_are_present():
    """Точка А и точка Б — два отдельных вопроса с явными ролями."""
    roles = _multi_questions()
    assert set(roles) == {"now", "want"}, f"роли multi-вопросов: {sorted(roles)}"


def test_both_points_use_the_same_ruler():
    """Один список на обе точки — как вопросы 4 и 5 анкеты."""
    roles = _multi_questions()
    assert sorted(roles["now"]) == sorted(roles["want"])


@pytest.mark.parametrize("role", ["now", "want"])
def test_quiz_words_match_the_lexicon_short_set(role):
    """Слова квиза — ровно выборка из core/lexicon, без самодеятельности."""
    assert sorted(_multi_questions()[role]) == sorted(lx.SHORT_SET)


@pytest.mark.parametrize("role", ["now", "want"])
def test_quiz_words_are_balanced_across_fields(role):
    """4 поля по 3 слова. Перекос сместил бы и Формулу, и Gap."""
    words = _multi_questions()[role]
    assert Counter(lx.field_of(w) for w in words) == {f: 3 for f in lx.FIELDS}


@pytest.mark.parametrize("role", ["now", "want"])
def test_declared_field_matches_the_lexicon(role):
    """Разметка wantField в квизе совпадает с полем слова в лексиконе."""
    src = QUIZ.read_text(encoding="utf-8")
    for block in re.findall(r"\{\s*id:\s*\d+,\s*block:.*?\n\s*\},", src, re.S):
        if 'kind: "multi"' not in block or f'role: "{role}"' not in block:
            continue
        for word, field in re.findall(r'\{\s*t:\s*"([^"]+)",\s*wantField:\s*"(\w+)"', block):
            assert lx.field_of(word) == field, f"«{word}» размечено {field}, в лексиконе {lx.field_of(word)}"


def test_point_a_no_longer_uses_words_outside_the_instrument():
    """Слова, которыми точка А собиралась раньше, в измерение больше не попадают.

    «спокойная», «деловая», «незаметная» и прочие описательные слова не входят в анкету школы:
    именно из-за них распределение не строилось и Gap приходил заглушкой.
    """
    src = QUIZ.read_text(encoding="utf-8")
    collect = re.search(r"function collectTraits\(\).*?\n\}", src, re.S).group(0)
    assert "opt.now" not in collect, "точка А обязана собираться выбором из лексикона, не opt.now"
    assert "role === 'now'" in collect, "роль вопроса должна разводить точки А и Б"
