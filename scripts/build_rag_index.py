"""Сборка RAG-индекса из architecture/reference/ (запускать офлайн, вектора коммитим).

Шаги:
  1) нарезка .md-файлов на чанки по заголовкам (### , иначе ## );
  2) авто-теги к каждому чанку (цветотип / фигура / семантическое поле / сезон) —
     по справочным enum'ам из formula-diagnostic;
  3) (опц.) эмбеддинги чанков локальной моделью -> data/rag/vectors.npz.

Чанки пишем всегда (data/rag/chunks.json). Эмбеддинги — если установлен fastembed
(лёгкий ONNX, без torch). Без эмбеддингов retrieval работает на тегах (фолбэк).

Запуск:  python -m scripts.build_rag_index           # чанки + (если есть) вектора
         python -m scripts.build_rag_index --no-embed  # только чанки
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "architecture" / "reference"
OUT_DIR = ROOT / "data" / "rag"

# какие папки реально питают диагностику (остальное — мета/README/сырьё)
INCLUDE_DIRS = [
    "colortypes", "figure-correction", "style-typology", "impression-lexicon",
    "prototypes", "print-mixing", "wardrobe", "image-psychology", "glossary",
    "request-diagnostics", "scenarios", "trends",
]
SKIP_NAMES = {"README.md"}

# ── словари тегов (enum'ы из formula-diagnostic) ─────────────────────────────
_SEASON = {"зим": "winter", "лет": "summer", "весн": "spring", "осен": "autumn"}
_TONE = {"контрастн": "contrast", "натуральн": "natural", "светл": "light"}
FIGURES = ["rectangle", "hourglass", "pear", "inverted_triangle", "apple"]
# Курс «Алгоритмы имиджа» называет фигуры иначе, чем англо-коды движка: «нижний тип» = груша,
# «верхний тип/треугольник» = перевёрнутый треугольник, «овал» = круг. Без этих синонимов куски
# методологии, написанные языком курса, не тегировались на фигуру и терялись в поиске.
_FIGURE_RU = {
    "песочные часы": "hourglass", "восьмёрка": "hourglass", "восьмерка": "hourglass",
    "перевёрнутый треугольник": "inverted_triangle", "перевернутый треугольник": "inverted_triangle",
    "верхний треугольник": "inverted_triangle", "верхний тип": "inverted_triangle",
    "груша": "pear", "нижний тип": "pear",
    "прямоугольник": "rectangle", "яблоко": "apple", "круг": "apple", "овал": "apple",
}
_FIELD_RU = {"классика": "classic", "натуральн": "natural",
             "романтика": "romance", "драма": "drama"}


def _colortypes(text: str) -> list[str]:
    """Найти коды цветотипов: по русским «Зима натуральная» и по готовым кодам."""
    low = text.lower()
    found = set()
    for code in ("spring", "summer", "autumn", "winter"):
        for tone in ("contrast", "natural", "light"):
            if f"{code}_{tone}" in low:
                found.add(f"{code}_{tone}")
    # русские пары «сезон + тон» в пределах одной строки заголовка/абзаца
    for line in low.splitlines():
        season = next((v for k, v in _SEASON.items() if k in line), None)
        tone = next((v for k, v in _TONE.items() if k in line), None)
        if season and tone:
            found.add(f"{season}_{tone}")
    return sorted(found)


def _figures(text: str) -> list[str]:
    low = text.lower()
    found = {f for f in FIGURES if f in low}
    found |= {code for ru, code in _FIGURE_RU.items() if ru in low}
    return sorted(found)


def _fields(text: str) -> list[str]:
    low = text.lower()
    return sorted({code for ru, code in _FIELD_RU.items() if ru in low})


_H_RE = re.compile(r"^(#{1,4})\s+(.*)$")


def _split_by_headers(md: str, level: int) -> list[tuple[str, str]]:
    """Разбить markdown на (заголовок, тело) по заголовкам указанного уровня."""
    lines = md.splitlines()
    chunks, cur_title, cur_body = [], None, []
    marker = "#" * level + " "
    for ln in lines:
        if ln.startswith(marker) and not ln.startswith("#" * (level + 1)):
            if cur_title is not None:
                chunks.append((cur_title, "\n".join(cur_body).strip()))
            cur_title = ln[len(marker):].strip()
            cur_body = []
        elif cur_title is not None:
            cur_body.append(ln)
    if cur_title is not None:
        chunks.append((cur_title, "\n".join(cur_body).strip()))
    return chunks


def chunk_file(path: Path) -> list[dict]:
    md = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()
    has_h3 = bool(re.search(r"^### ", md, re.MULTILINE))
    raw = _split_by_headers(md, 3 if has_h3 else 2)
    out = []
    for title, body in raw:
        if len(body) < 40:  # пустые/служебные секции пропускаем
            continue
        scope = f"{title}\n{body}"
        # цветотип/фигура: если есть в ЗАГОЛОВКЕ секции — он авторитетен (тело
        # часто упоминает соседние типы для сравнения → ложные теги). Иначе — по телу.
        colortype = _colortypes(title) or _colortypes(scope)
        figure = _figures(title) or _figures(scope)
        out.append({
            "id": f"{rel}#{title}"[:200],
            "source": rel,
            "section": title,
            "text": f"{title}. {body}",
            "tags": {
                "colortype": colortype,
                "figure": figure,
                "field": _fields(scope),
            },
        })
    return out


def build_chunks() -> list[dict]:
    chunks = []
    for d in INCLUDE_DIRS:
        for path in sorted((REF_DIR / d).glob("*.md")):
            if path.name in SKIP_NAMES:
                continue
            chunks.extend(chunk_file(path))
    return chunks


def embed_chunks(chunks: list[dict]) -> bool:
    """Посчитать вектора локально (fastembed). True если получилось."""
    try:
        import numpy as np
        from fastembed import TextEmbedding
    except ImportError:
        print("fastembed/numpy не установлены — пропускаю эмбеддинги (retrieval по тегам).")
        return False
    from core.rag import EMBED_MODEL
    model = TextEmbedding(model_name=EMBED_MODEL)
    vecs = list(model.embed([c["text"] for c in chunks]))
    arr = np.array(vecs, dtype="float32")
    arr /= (np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9)  # нормализуем для cosine
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT_DIR / "vectors.npz", vectors=arr,
                        ids=np.array([c["id"] for c in chunks]))
    print(f"Вектора: {arr.shape} -> {OUT_DIR / 'vectors.npz'}")
    return True


def main() -> None:
    chunks = build_chunks()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "chunks.json").write_text(
        json.dumps(chunks, ensure_ascii=False, indent=1), encoding="utf-8")
    tagged = sum(1 for c in chunks if any(c["tags"].values()))
    print(f"Чанков: {len(chunks)} (с тегами: {tagged}) -> {OUT_DIR / 'chunks.json'}")
    if "--no-embed" not in sys.argv:
        embed_chunks(chunks)


if __name__ == "__main__":
    main()
