"""LLM-судья качества образа.

Оценивает, насколько сгенерированный образ соответствует диагностике и сохраняет
личность клиентки. Даёт объективные метрики для eval (Фаза 3) — без ручной разметки.
"""
from __future__ import annotations
import json

from core import config, provider

JUDGE_PROMPT = """You are a strict, objective auditor for a methodological AI styling system.
You receive a REFERENCE photo of a real client, a GENERATED styled look image, and the
diagnosed style formula (JSON). Judge ONLY against the diagnosis and identity preservation.
Return ONLY valid JSON, no prose:
{
  "palette_fidelity": <float 0..1: outfit uses the diagnosed base+accent colors, no taboo/stop colors>,
  "accent_present": <true|false: the diagnosed accent color is clearly present>,
  "figure_fit": <float 0..1: silhouette respects figure-correction (e.g. a defined waist for a rectangle; none of the stop_list items)>,
  "identity_similarity": <float 0..1: same woman as the reference — face, hair, body proportions>,
  "overall": <float 0..1: overall fidelity of the look to the diagnosis>,
  "notes": "<one short sentence, in Russian>"
}"""

_KEYS = ["style_formula", "figure_type", "colortype", "tonal_characteristics", "visual_formula"]


def judge_look(look_image: str, diagnosis: dict, reference_photo: str | None = None,
               model: str | None = None) -> dict:
    """look_image — путь/data-URL образа; diagnosis — выход formula-diagnostic.
    reference_photo — фото клиентки для оценки сходства. Возвращает метрики (dict)."""
    model = model or config.model_for("vision", "dev")  # vision-модель как судья
    content: list = []
    if reference_photo:
        content += [provider.text_block("REFERENCE client photo:"),
                    provider.image_block(reference_photo)]
    content += [provider.text_block("GENERATED look to audit:"),
                provider.image_block(look_image)]
    content.append(provider.text_block(
        "Diagnosed formula JSON:\n" + json.dumps(_judge_input(diagnosis), ensure_ascii=False)
    ))
    return provider.chat_json(model, JUDGE_PROMPT, content, max_tokens=800)


def _judge_input(d: dict) -> dict:
    return {k: d.get(k) for k in _KEYS if d.get(k) is not None}
