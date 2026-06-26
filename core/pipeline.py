"""Сквозной пайплайн: vision-анализ → диагностика Формулы стиля → (капсула).

Шаги 1-2 (vision + диагностика) реализованы. Шаг 3 (look-generator + генерация
образов через Seedream) подключается в Фазе 2 — см. plans/2026-06-25-mvp-vertical-slice.md.
"""
from __future__ import annotations
import json

from . import config, provider
from .prompts import load_knowledge, load_system_prompt


def analyze_photos(image_paths, height_cm: int | None = None, mode: str | None = None) -> dict:
    """Шаг 1. Vision: фото клиентки → JSON (цветотип, контраст, палитра, фигура)."""
    system = load_system_prompt("vision-analyzer")
    content = [provider.image_block(p) for p in image_paths]
    if height_cm:
        content.append(provider.text_block(json.dumps({"height_cm": height_cm}, ensure_ascii=False)))
    return provider.chat_json(config.model_for("vision", mode), system, content, max_tokens=2048)


def diagnose(quiz_answers: dict, vision_result: dict, mode: str | None = None) -> dict:
    """Шаг 2. Диагностика: ответы квиза + выход vision → Формула стиля + Identity Gap."""
    system = load_system_prompt("formula-diagnostic")
    payload = {**quiz_answers, **_vision_to_diagnostic_input(vision_result)}
    return provider.chat_json(
        config.model_for("text", mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )


def _vision_to_diagnostic_input(v: dict) -> dict:
    """Стыковка по таблице из vision-analyzer.md ('Как стыкуется с движком')."""
    figure = v.get("figure") or {}
    return {
        "tonal_characteristics": v.get("tonal_characteristics"),
        "colortype": v.get("colortype"),
        "natural_palette": v.get("natural_palette"),
        "figure_type": figure.get("figure_type"),
        "correction_flags": figure.get("correction_flags"),
    }


def generate_capsule(diagnosis: dict, generation_request: dict, mode: str | None = None) -> dict:
    """Шаг 3. Капсула: Формула стиля + запрос → капсула вещей + образы с промптами для генерации.

    Системный промпт look-generator требует подклеенный целиком style-library (knowledge base).
    Каждый образ на выходе содержит image_generation_prompt — он пойдёт в Seedream (Фаза 2).
    """
    system = (
        load_system_prompt("look-generator")
        + "\n\n# БАЗА ЗНАНИЙ (style-library)\n\n"
        + load_knowledge("style-library")
    )
    payload = {
        "style_formula_result": _diagnosis_to_formula_result(diagnosis),
        "generation_request": generation_request,
    }
    return provider.chat_json(
        config.model_for("text", mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )


def _diagnosis_to_formula_result(d: dict) -> dict:
    """Стыковка выхода formula-diagnostic со входом look-generator (style_formula_result)."""
    return {
        "style_formula": d.get("style_formula"),
        "base_style": d.get("base_style"),
        "primary_substyle": d.get("primary_substyle"),
        "secondary_substyle": d.get("secondary_substyle"),
        "accent_note": d.get("accent_note"),
        "figure_type": d.get("figure_type"),
        "tonal_characteristics": d.get("tonal_characteristics"),
        "visual_formula": d.get("visual_formula"),
    }
