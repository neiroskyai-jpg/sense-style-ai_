"""Загрузка системных промптов из architecture/prompts/*.md.

Каждый промпт-файл содержит секцию '## SYSTEM PROMPT'. Встречаются два формата:
  1) промпт целиком обёрнут в один fenced-блок (vision-analyzer, formula-diagnostic…).
     ВАЖНО: внутри блока есть свои markdown-подзаголовки '## ' (части методологии),
     поэтому блок берём ЦЕЛИКОМ до закрывающего ```, не обрезая по '## ';
  2) промпт идёт прозой после заголовка, со своими '###' подсекциями (style-book) —
     берём текст до следующего заголовка уровня '## '.
Источник правды по промптам — architecture/prompts/, синхронен с методологией.
"""
from __future__ import annotations
import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "architecture" / "prompts"

_HEADER_RE = re.compile(r"##\s*SYSTEM\s*PROMPT[^\n]*\n", re.IGNORECASE)
# fenced-блок сразу после заголовка (формат 1): берём до ПЕРВОГО закрывающего ```
_FENCE_RE = re.compile(r"\s*```[^\n]*\n(.*?)\n```", re.DOTALL)
# следующий заголовок уровня '## ' (для формата 2)
_NEXT_H2_RE = re.compile(r"^##\s", re.MULTILINE)


@lru_cache(maxsize=None)
def load_system_prompt(name: str) -> str:
    """name — имя файла без .md, напр. 'vision-analyzer' или 'formula-diagnostic'."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Промпт не найден: {path}")
    text = path.read_text(encoding="utf-8")

    header = _HEADER_RE.search(text)
    if not header:
        raise ValueError(f"В {path.name} не найден заголовок '## SYSTEM PROMPT'")

    body = text[header.end():]

    if body.lstrip().startswith("```"):
        # формат 1: единый fenced-блок — берём его содержимое целиком
        fence = _FENCE_RE.match(body)
        if not fence:
            raise ValueError(f"В {path.name} не закрыт fenced-блок '## SYSTEM PROMPT'")
        prompt = fence.group(1).strip()
    else:
        # формат 2: проза до следующего заголовка '## '
        nxt = _NEXT_H2_RE.search(body)
        prompt = (body[:nxt.start()] if nxt else body).strip().strip("-").strip()

    if len(prompt) < 500 or prompt.startswith("```"):
        raise ValueError(
            f"В {path.name} промпт извлечён некорректно (len={len(prompt)}) — проверь структуру файла"
        )
    return prompt
