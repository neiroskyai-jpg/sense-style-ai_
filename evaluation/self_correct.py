"""Агентный self-correction: рендер образа с проверкой судьёй и авто-перегенерацией.

Конвейер не «выстрелил и забыл», а проверяет результат против диагностики (LLM-судья)
и перегенерирует, если образ уплыл по палитре/фигуре/личности. Возвращает лучший
вариант с метриками и флагом, прошёл ли он порог.
"""
from __future__ import annotations

from core.pipeline import render_look_on_client

from .judge import judge_look


def render_look_validated(client_photo: str, look_prompt: str, diagnosis: dict,
                          threshold: float = 0.7, max_attempts: int = 2) -> dict:
    """Рендер с обратной связью судьи. Возвращает
    {'img', 'scores', 'attempt', 'accepted'} — лучший из попыток."""
    best: dict | None = None
    for attempt in range(1, max_attempts + 1):
        img = render_look_on_client(client_photo, look_prompt)
        scores = judge_look(img, diagnosis, reference_photo=client_photo)
        cand = {"img": img, "scores": scores, "attempt": attempt}
        if best is None or scores.get("overall", 0) > best["scores"].get("overall", 0):
            best = cand
        passed = (scores.get("overall", 0) >= threshold
                  and scores.get("identity_similarity", 0) >= threshold)
        if passed:
            return {**cand, "accepted": True}
    return {**best, "accepted": False}
