"""Тесты загрузчика промптов — без обращения к API (гоняются в CI бесплатно)."""
import pytest

from core.prompts import load_system_prompt

# все промпт-файлы, у которых есть блок '## SYSTEM PROMPT'
SYSTEM_PROMPTS = [
    "vision-analyzer",
    "formula-diagnostic",
    "look-generator",
    "shopping-list",
    "style-book",
]


@pytest.mark.parametrize("name", SYSTEM_PROMPTS)
def test_system_prompt_loads(name):
    prompt = load_system_prompt(name)
    assert isinstance(prompt, str)
    # реальные системные промпты — это килобайты; короче 800 = обрезка (был баг с '## ')
    assert len(prompt) > 800, f"{name}: промпт подозрительно короткий ({len(prompt)})"
    # не должно быть висящего открывающего fence — признак неполного извлечения
    assert not prompt.startswith("```"), f"{name}: в промпт попал маркер ```"


def test_formula_diagnostic_full_body():
    """Защита от регресса: у formula-diagnostic есть '## '-подзаголовки ВНУТРИ блока,
    из-за чего ранее промпт обрезался. Проверяем, что тело методологии на месте."""
    prompt = load_system_prompt("formula-diagnostic")
    assert len(prompt) > 5000, f"formula-diagnostic обрезан: {len(prompt)}"
    assert "подстил" in prompt, "в промпте нет блока подстилей — обрезка"


def test_missing_prompt_raises():
    with pytest.raises(FileNotFoundError):
        load_system_prompt("no-such-prompt")
