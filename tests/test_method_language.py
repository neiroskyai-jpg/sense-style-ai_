"""Проработанная методология обязана доезжать до генерации.

Разбор библиотеки курса «Алгоритмы имиджа» показал: раздел 8 метода «Словарь языка: что говорим,
что не говорим» был написан, но НЕ использовался в коде ни разу. Тексты клиентке писались обычным
языком стилиста — «тип фигуры Прямоугольник», «скрыть недостатки», — хотя метод требует бережного
языка (Mair, 2025). Раздел 4 «Сводная карта 4 чистых стилей» тоже не попадал в генерацию: модель
знала ярлык «Классика», но не ткани, линию плеча и стоп-лист этого стиля.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402


def test_method_section_extracts_one_section():
    assert p._method_section(8).lstrip().startswith("## 8.")
    assert "## 9." not in p._method_section(8)


def test_language_dictionary_is_loaded():
    """Словарь языка — не декларация в документе, а рабочая часть промпта."""
    lang = p._language_reference()

    assert "Словарь языка" in lang
    assert "НЕ говорим" in lang and "ГОВОРИМ" in lang
    assert "Работать со своими пропорциями" in lang


def test_pure_styles_reference_carries_attributes():
    """Чистый стиль — это ткани, силуэт и стоп-лист, а не только название."""
    pure = p._pure_styles_reference()

    assert "Сводная карта 4 чистых стилей" in pure
    for marker in ("Ткани", "Силуэт", "Стоп", "КЛАССИКА", "ДРАМА", "РОМАНТИКА", "НАТУРАЛЬНЫЙ"):
        assert marker in pure, marker


def test_capsule_prompt_carries_method_and_language(monkeypatch):
    """Сборка капсулы получает и подстили, и чистые стили, и словарь языка."""
    seen = {}

    def fake_chat_json(model, system, user, **kw):
        seen["system"] = system
        return {"looks": [], "capsule": {}}

    monkeypatch.setattr(p.provider, "chat_json", fake_chat_json)
    p.generate_capsule({"style_formula": "Классика"}, {"mode": "capsule"})

    system = seen["system"]
    assert "25 ПОДСТИЛЕЙ" in system
    assert "4 ЧИСТЫХ СТИЛЯ" in system
    assert "ЯЗЫК ТЕКСТОВ ДЛЯ КЛИЕНТКИ" in system


def test_diagnosis_prompt_carries_language(monkeypatch):
    """Диагностика пишет клиентке первый текст о ней самой — язык там задаёт тон всему продукту."""
    seen = {}

    def fake_chat_json(model, system, user, **kw):
        seen["system"] = system
        return {"style_formula": "Классика", "gap_percentage": 40}

    monkeypatch.setattr(p.provider, "chat_json", fake_chat_json)
    p.diagnose({"now_traits": [], "want_traits_top3": []}, {})

    assert "ЯЗЫК ТЕКСТОВ ДЛЯ КЛИЕНТКИ" in seen["system"]
