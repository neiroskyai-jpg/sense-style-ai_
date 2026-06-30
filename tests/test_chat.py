"""Тесты чат-стилиста — без API (проверяем сборку контекста, не сам вызов модели)."""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import chat  # noqa: E402


def test_profile_block_without_formula_invites_diagnosis():
    block = chat._profile_block(None)
    assert "нет" in block.lower() and "диагностик" in block.lower()


def test_profile_block_with_formula_includes_it():
    profile = {"diagnosis": {"style_formula": "Классика × Драма", "colortype": "winter_natural",
                             "figure_type": "rectangle", "gap_percentage": 60,
                             "visual_formula": {"stop_list": ["пастель"]}}}
    block = chat._profile_block(profile)
    assert "Классика × Драма" in block
    assert "winter_natural" in block


def test_system_prompt_carries_tone_and_navigation():
    # базовый системный промпт содержит правила тона и знание лестницы продуктов
    assert "на «ты»" in chat._BASE
    assert "КАРТА СТИЛЯ" in chat._BASE
    assert "восклицательных" in chat._BASE
