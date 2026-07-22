"""Канон метода: 4 стиля и 25 подстилей. Ничего сверх этого списка не существует.

Зачем отдельный модуль. Генеративные шаги (диагностика, уточнение подстиля, направления,
капсула) свободно придумывали ярлыки: клиентка получала «Нежная Реформаторская», «Структурный
Романтизм», «Soft Classic», «Драма-акцент». Слова знакомые, но таких подстилей в методе нет —
и рядом со своей Формулой клиентка видит вторую, чужую диагностику. Продукт при этом обещает
один метод.

Здесь мы держим единственный источник правды по названиям (`sense-style-method.md`), правило для
промптов и функцию, которая возвращает выдуманный ярлык в канон.
"""
from __future__ import annotations

import re
import sys

from .prompts import load_reference

# 4 стиля-поля метода. Подстиль всегда уточняет одно из них.
STYLES = ("Классика", "Драма", "Романтика", "Натуральный")

_SUBSTYLES_CACHE: list[str] | None = None
_RULE_CACHE: str | None = None


def _clean(raw: str) -> str:
    """«# 3. Денди-Гарсон — маскулинная классика» → «Денди-Гарсон»."""
    s = raw.strip().lstrip("#").strip()
    s = re.sub(r"^\d+\.\s*", "", s)          # порядковый номер
    s = re.split(r"\s+[—–-]\s+", s)[0]       # пояснение после тире
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)   # скобочное уточнение в конце
    return s.strip()


def substyles() -> list[str]:
    """25 канонических имён подстилей — ровно как в методе."""
    global _SUBSTYLES_CACHE
    if _SUBSTYLES_CACHE is not None:
        return _SUBSTYLES_CACHE
    try:
        text = load_reference("sense-style-method.md")
    except FileNotFoundError:
        _SUBSTYLES_CACHE = []
        return []
    m = re.search(r"\n## 5\. 25 уточняющих подстилей\n", text)
    section = text[m.end():].split("\n## ")[0] if m else ""
    names = []
    for raw in re.findall(r"^###\s*(.+)$", section, re.M):
        # заголовки-группы («Нюансы Классики (8 подстилей)») — не подстили
        if re.search(r"подстил", raw, re.I):
            continue
        name = _clean(raw)
        if name and name not in names:
            names.append(name)
    _SUBSTYLES_CACHE = names
    return names


def canon_rule() -> str:
    """Блок для системных промптов: запрет выдумывать названия + сам список."""
    global _RULE_CACHE
    if _RULE_CACHE is not None:
        return _RULE_CACHE
    names = substyles()
    listed = "; ".join(names) if names else "(список подстилей недоступен)"
    _RULE_CACHE = (
        "# КАНОН МЕТОДА — НИЧЕГО НЕ ВЫДУМЫВАТЬ (обязательно к соблюдению)\n"
        "В методе ровно 4 стиля и 25 подстилей. Других стилей и подстилей НЕ СУЩЕСТВУЕТ.\n"
        f"4 стиля: {', '.join(STYLES)}.\n"
        f"25 подстилей: {listed}.\n"
        "Правила именования:\n"
        "1. Название подстиля бери ТОЛЬКО из списка выше, слово в слово.\n"
        "2. Не придумывай новых названий и не склеивай слова стилей в новые ярлыки: "
        "«Структурный Романтизм», «Нежная Реформаторская», «Soft Classic», «Драма-акцент» — "
        "так нельзя, этого в методе нет.\n"
        "3. Если подходящего подстиля в списке нет, назови СТИЛЬ (одно из четырёх полей) — "
        "это честнее выдуманного ярлыка.\n"
        "4. Свойства образа описывай обычными словами («структурный силуэт», «мягкая линия»), "
        "не превращая их в название стиля с заглавных букв.\n"
        # Три подстиля метода названы по-английски, и в тексте для клиентки это читается как чужое:
        # «Мягкое прочтение Power Woman». Подстановка русского имени задним числом ломает падеж,
        # поэтому русское имя должна писать сама модель — фразу она строит целиком.
        "5. В ТЕКСТАХ ДЛЯ КЛИЕНТКИ три подстиля называй по-русски: Power Woman — «Сильная "
        "женщина», Quiet Luxury — «Тихая роскошь», Smart Casual — «Смарт-кэжуал». Согласуй "
        "падеж: «мягкое прочтение Сильной женщины», а не «прочтение Сильная женщина». "
        "В служебных полях (primary_substyle, secondary_substyle) оставляй каноническое "
        "английское имя из списка.\n"
    )
    return _RULE_CACHE


# Ярлыки, которые модель реально выдавала вместо канонических имён (наблюдения 17–19.07.2026).
# По словам их не сопоставить — они англоязычные или склеены заново, поэтому держим таблицу.
_ALIASES = {
    "softclassic": "Чистая классика",
    "мягкаяклассика": "Чистая классика",
    "драмаакцент": "Феминная драма",
    "softdrama": "Феминная драма",
    "casualchic": "Парижский шик",
    "frenchchic": "Парижский шик",
    "modernclassic": "Чистая классика",
    "businesscasual": "Smart Casual",
    "powerdressing": "Power Woman",
}


def _norm(s: str) -> str:
    return re.sub(r"[^а-яёa-z]+", "", (s or "").lower())


def snap_substyle(name: str) -> str:
    """Вернуть выдуманный ярлык в канон. Пусто — если сопоставить не с чем.

    Точное и вхождение — надёжные случаи. Дальше сравниваем по общим словам: «Леди-лайк
    элегантный» → «Леди-лайк». Ничего не совпало — возвращаем пусто, и вызывающий код решает,
    показать ли стиль вместо подстиля.
    """
    name = (name or "").strip()
    if not name:
        return ""
    names = substyles()
    if not names:
        return name                       # метода нет под рукой — не выдумываем и не ломаем
    n = _norm(name)
    if n in _ALIASES:
        return _ALIASES[n]
    for c in names:
        if _norm(c) == n:
            return c
    for c in names:
        cn = _norm(c)
        if cn and (cn in n or n in cn):
            return c
    words = {w for w in re.findall(r"[а-яёa-z]{4,}", name.lower())}
    best, score = "", 0
    for c in names:
        cw = {w for w in re.findall(r"[а-яёa-z]{4,}", c.lower())}
        overlap = len(words & cw)
        if overlap > score:
            best, score = c, overlap
    return best if score else ""


def enforce_substyles(diagnosis: dict) -> dict:
    """Привести подстили диагностики к канону. Расхождения пишем в лог — это метрика дрейфа."""
    for key in ("primary_substyle", "secondary_substyle"):
        raw = (diagnosis.get(key) or "").strip()
        if not raw:
            continue
        snapped = snap_substyle(raw)
        if snapped and snapped != raw:
            print(f"[canon] {key}: «{raw}» → «{snapped}» (подстиль вне метода)", file=sys.stderr)
            diagnosis[key] = snapped
        elif not snapped:
            # Совсем не опознали: лучше назвать стиль, чем показать клиентке выдумку.
            base = (diagnosis.get("base_style") or "").strip()
            print(f"[canon] {key}: «{raw}» вне метода, замен не нашлось "
                  f"(base_style={base or '—'})", file=sys.stderr)
    return diagnosis


# Три подстиля метода названы по-английски: Power Woman, Quiet Luxury, Smart Casual. В данных
# держим канон — по нему работают enforce_substyles и промпты. Но на экране клиентки английское
# слово читается как чужое: лендинг давно говорит «Сильная женщина», а продукт показывал
# «Мягкое прочтение Power Woman». Перевод — только слой показа, в хранилище ничего не меняем.
_RU_DISPLAY = {
    "Power Woman": "Сильная женщина",
    "Quiet Luxury": "Тихая роскошь",
    "Smart Casual": "Смарт-кэжуал",
}


def ru_display(text) -> str:
    """Русские имена подстилей для показа клиентке. Не для записи в профиль."""
    out = str(text or "")
    for en, ru in _RU_DISPLAY.items():
        out = out.replace(en, ru)
    return out
