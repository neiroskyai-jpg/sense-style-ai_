"""Локальный RAG по авторской базе знаний (architecture/reference/).

Гибридный retrieval:
  • теги — точная фильтрация по профилю (цветотип / фигура / семантическое поле),
    работает ВСЕГДА, без зависимостей;
  • семантика — cosine по эмбеддингам (fastembed, ONNX), если индекс собран и
    библиотека установлена; иначе тихо деградируем до тегов.

Индекс собирается офлайн: `python -m scripts.build_rag_index` (вектора коммитим).
Вход retrieve() — профиль из диагностики; выход — правила с пометкой, чем совпали
(для подмешивания в промпт и для блока объяснимости «почему ИИ так решил»).
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

_DATA = Path(__file__).resolve().parent.parent / "data" / "rag"
_CHUNKS = _DATA / "chunks.json"
_VECTORS = _DATA / "vectors.npz"

# веса совпадений по тегам: цветотип и фигура — сильные сигналы, поле — слабее
_W = {"colortype": 1.0, "figure": 1.0, "field": 0.45}
_SEM_WEIGHT = 0.8  # вклад семантики в общий скор (когда вектора есть)
_PER_FOLDER = 2    # не более N правил из одной папки — для разнообразия выдачи

# приоритет источников: канон метода (цветотип/фигура/типология) важнее трендов
_FOLDER_PRIOR = {
    "colortypes": 1.0, "figure-correction": 1.0, "style-typology": 0.85,
    "impression-lexicon": 0.7, "prototypes": 0.6, "image-psychology": 0.5,
    "print-mixing": 0.45, "wardrobe": 0.45, "request-diagnostics": 0.4,
    "scenarios": 0.35, "trends": 0.3, "glossary": 0.3,
}


def _folder(source: str) -> str:
    parts = source.split("/")
    return parts[2] if len(parts) > 2 else parts[-1]

# человекочитаемые названия источников для блока объяснимости
_SOURCE_RU = {
    "colortypes": "цветотип", "figure-correction": "коррекция фигуры",
    "style-typology": "типология стиля", "impression-lexicon": "лексикон впечатления",
    "prototypes": "стилевой ориентир", "print-mixing": "сочетание принтов",
    "wardrobe": "рациональный гардероб", "image-psychology": "психология образа",
    "scenarios": "сценарии", "trends": "тренды", "glossary": "глоссарий",
    "request-diagnostics": "диагностика запроса",
}


@lru_cache(maxsize=1)
def _chunks() -> list[dict]:
    if not _CHUNKS.exists():
        return []
    return json.loads(_CHUNKS.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _vectors():
    """(ids, matrix) или None. numpy/индекс могут отсутствовать — это ок."""
    if not _VECTORS.exists():
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    data = np.load(_VECTORS, allow_pickle=True)
    return list(data["ids"]), data["vectors"]


@lru_cache(maxsize=1)
def _embedder():
    """Ленивая fastembed-модель. None → фолбэк на теги.

    Ловим не только ImportError: модель кэшируется в системном TEMP и может быть удалена/побита
    (onnxruntime NoSuchFile), а в CI её просто нет. Семантика — усиление, а не обязательное звено.
    """
    try:
        from fastembed import TextEmbedding
        return TextEmbedding(model_name=EMBED_MODEL)
    except Exception:  # noqa: BLE001 — RAG не должен ронять диагностику из-за модели
        return None


def _embed_query(text: str):
    model = _embedder()
    if model is None:
        return None
    try:
        import numpy as np
        vec = next(iter(model.embed([text])))
        vec = np.asarray(vec, dtype="float32")
        return vec / (np.linalg.norm(vec) + 1e-9)
    except Exception:  # noqa: BLE001 — битая модель/рантайм → тихий фолбэк на теги
        return None


def _profile_tags(profile: dict) -> dict:
    """Из профиля диагностики собрать целевые теги для фильтрации."""
    dist = profile.get("semantic_field_distribution") or {}
    fields = [k for k, _ in sorted(dist.items(), key=lambda kv: kv[1], reverse=True)
              if dist.get(k, 0) > 0][:2]
    return {
        "colortype": [profile.get("colortype")] if profile.get("colortype") else [],
        "figure": [profile.get("figure_type")] if profile.get("figure_type") else [],
        "field": fields or ([profile.get("base_style")] if profile.get("base_style") else []),
    }


def _profile_query(profile: dict) -> str:
    """Текст запроса для семантического поиска (формула + желаемое впечатление)."""
    parts = [
        profile.get("style_formula") or "",
        profile.get("primary_substyle") or "",
        profile.get("secondary_substyle") or "",
        " ".join(profile.get("want_traits_top3") or []),
        profile.get("colortype") or "",
        profile.get("figure_type") or "",
    ]
    return ", ".join(p for p in parts if p)


def retrieve(profile: dict, k: int = 6) -> list[dict]:
    """Топ-k правил из базы под профиль. Каждое: id, source, section, text,
    matched (какие теги совпали), score. Работает и без эмбеддингов (по тегам)."""
    chunks = _chunks()
    if not chunks:
        return []
    target = _profile_tags(profile)

    # семантические скоры (если есть вектора и эмбеддер)
    sem = {}
    vecs = _vectors()
    qv = _embed_query(_profile_query(profile)) if vecs else None
    if vecs is not None and qv is not None:
        ids, matrix = vecs
        scores = matrix @ qv  # косинус (всё нормировано)
        sem = {ids[i]: float(scores[i]) for i in range(len(ids))}

    ranked = []
    for c in chunks:
        tags = c.get("tags") or {}
        matched = {}
        tag_score = 0.0
        for dim, weight in _W.items():
            hits = [t for t in tags.get(dim, []) if t in target.get(dim, [])]
            if hits:
                matched[dim] = hits
                tag_score += weight * len(hits)
        prior = _FOLDER_PRIOR.get(_folder(c["source"]), 0.4)
        # канон важнее трендов: тег-скор взвешиваем приоритетом папки, семантику добавляем
        score = tag_score * prior + _SEM_WEIGHT * sem.get(c["id"], 0.0)
        if tag_score > 0 or sem.get(c["id"], 0.0) > 0.25:
            ranked.append((score, matched, c))

    ranked.sort(key=lambda x: x[0], reverse=True)
    out, per_folder = [], {}
    for score, matched, c in ranked:
        fld = _folder(c["source"])
        if per_folder.get(fld, 0) >= _PER_FOLDER:  # разнообразие источников
            continue
        per_folder[fld] = per_folder.get(fld, 0) + 1
        out.append({
            "id": c["id"], "source": c["source"], "section": c["section"],
            "text": c["text"], "matched": matched, "score": round(score, 3),
            "source_label": _SOURCE_RU.get(fld, fld),
        })
        if len(out) >= k:
            break
    return out


def rules_block(rules: list[dict], max_chars: int = 6000) -> str:
    """Найденные правила → текст для подмешивания в системный промпт диагностики."""
    blocks, total = [], 0
    for r in rules:
        piece = f"### [{r['source_label']}] {r['section']}\n{r['text']}"
        if total + len(piece) > max_chars:
            break
        blocks.append(piece)
        total += len(piece)
    return "\n\n".join(blocks)


def cited_rules(rules: list[dict], limit: int = 4) -> list[dict]:
    """Компактный список «сработавших правил» для блока объяснимости (без длинных тел)."""
    out = []
    for r in rules[:limit]:
        snippet = r["text"]
        # тело без повтора заголовка, короткой выжимкой
        body = snippet.split(". ", 1)[1] if ". " in snippet else snippet
        out.append({
            "label": r["source_label"],
            "section": r["section"],
            "snippet": (body[:160] + "…") if len(body) > 160 else body,
            "matched": r["matched"],
        })
    return out
