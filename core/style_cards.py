"""Карточки коммерческих и исторических стилей — содержание вместо ярлыка.

Зачем модуль. В промпт генерации ехали 25 подстилей маркерами и 4 чистых стиля атрибутами, но
самих стилей — милитари, сафари, гаучо, рустика, авангарда — там не было ни в каком виде.
Модель видела слово «Милитари» и наполняла его как умела: получались хаки-оттенки без единой
знаковой вещи стиля. Здесь ярлык получает состав: цвета, принты, ткани, конкретные вещи, обувь.

Карточки подклеиваем не все (документ большой), а под подстили Формулы клиентки. Правила микса
и запреты — всегда: они не зависят от Формулы и нарушаются чаще всего.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .prompts import load_reference

_DOC = "reference/style-typology/commercial-historical-styles.md"
# Разделы, которые едут в промпт всегда: три правила прочтения и матрица микса с запретами.
_ALWAYS = ("0. Три правила", "3. Правила микса")


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    """Разделы верхнего уровня документа по заголовку '## '."""
    text = load_reference(_DOC)
    out: dict[str, str] = {}
    parts = re.split(r"\n## ", "\n" + text)
    for p in parts[1:]:
        title, _, body = p.partition("\n")
        out[title.strip()] = body.strip()
    return out


@lru_cache(maxsize=1)
def _cards() -> list[tuple[str, frozenset[str], str]]:
    """Карточки стилей: (название, подстили метода, текст).

    Подстили читаем из самой карточки («**Подстили метода:** Преппи»), а не из словаря в коде:
    иначе документ и код разъезжаются, и правка справочника молча перестаёт доезжать.
    """
    body = _sections().get("2. Карточки", "")
    out = []
    for p in re.split(r"\n### ", "\n" + body)[1:]:
        title, _, card = p.partition("\n")
        m = re.search(r"\*\*Подстили метода:\*\*\s*(.+)", card)
        names = {n.strip() for n in m.group(1).split(",")} if m else set()
        out.append((title.strip(), frozenset(names), ("### " + title.strip() + "\n" + card).strip()))
    return out


def known_substyles() -> set[str]:
    """Подстили метода, для которых в справочнике есть карточка стиля."""
    return {n for _, names, _ in _cards() for n in names}


def style_cards_prompt(*substyles: str | None) -> str:
    """Блок для системного промпта: правила прочтения + карточки под Формулу + запреты.

    Без подстилей возвращаем только правила: они действуют всегда, даже когда стиль клиентки
    описан одним чистым полем и отдельной карточки под него нет.
    """
    wanted = {s.strip() for s in substyles if s and s.strip()}
    # по префиксу, а не по точному имени: заголовок в документе несёт хвост
    # («## 0. Три правила, которые важнее карточек»), и правка формулировки не должна
    # молча выбрасывать раздел из промпта
    blocks = [b for t, b in _sections().items() if t.startswith(_ALWAYS)]
    picked = [card for _, names, card in _cards() if names & wanted]
    if picked:
        blocks.insert(1, "\n\n".join(picked))
    return "\n\n".join(b for b in blocks if b)
