"""Шаг колориста должен доезжать до сборки образов.

Палитра считается ДО капсулы, но раньше в генератор образов не передавалась: клиентка видела в
Карте одну палитру, а образы собирались в других цветах — вплоть до тех, что её гасят
(«графит на мягком лете»). Стоп-цвета не передавались вовсе.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as pl  # noqa: E402

DIAG = {
    "style_formula": "Классика × Драма",
    "figure_type": "hourglass",
    "visual_formula": {"silhouettes": ["Полуприлегающий"], "palette": ["беж"]},
    "tonal_characteristics": {"contrast": "medium"},
}
REQ = {
    "mode": "capsule", "season": "fw", "scenarios": ["деловая встреча"], "n_looks": 1,
    "palette": [
        {"name": "Айвори", "hex": "#F2E8D8", "group": "base"},
        {"name": "Бордо", "hex": "#6D1F2E", "group": "accent"},
    ],
    "stop_colors": [{"name": "Неон", "hex": "#D8E04A", "why": "спорит с подтоном"}],
}


def _system_prompt(monkeypatch) -> str:
    seen = {}
    monkeypatch.setattr(pl.provider, "chat_json",
                        lambda model, system, user, **kw: seen.setdefault("s", system) and {} or {})
    pl.generate_capsule(DIAG, REQ, mode="dev")
    return seen["s"]


def test_palette_colors_are_in_the_prompt(monkeypatch):
    system = _system_prompt(monkeypatch)

    assert "ПАЛИТРА КЛИЕНТКИ" in system
    assert "Айвори" in system and "Бордо" in system
    assert "База и нейтрали" in system and "Акценты" in system


def test_stop_colors_are_forbidden_explicitly(monkeypatch):
    system = _system_prompt(monkeypatch)

    assert "СТОП-ЦВЕТА" in system
    assert "Неон" in system
    assert "не использовать никогда" in system


def test_figure_and_denim_rules_still_reach_the_prompt(monkeypatch):
    """Правила посадки и матрица моделей низа — тоже часть персонального стайлинга."""
    system = _system_prompt(monkeypatch)

    assert "ПОСАДКА ПОД ФИГУРУ" in system
    assert "Модель низа" in system, "матрица джинсов по фигуре не доехала до генерации"
