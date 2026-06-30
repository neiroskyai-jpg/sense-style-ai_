"""Сквозной пайплайн: vision-анализ → диагностика Формулы стиля → (капсула).

Шаги 1-2 (vision + диагностика) реализованы. Шаг 3 (look-generator + генерация
образов через Seedream) подключается в Фазе 2 — см. plans/2026-06-25-mvp-vertical-slice.md.
"""
from __future__ import annotations
import json
from urllib.parse import quote_plus

from . import config, provider
from .prompts import load_knowledge, load_reference, load_system_prompt

try:
    from . import rag
except Exception:  # noqa: BLE001 — RAG-модуль/зависимости могут отсутствовать в проде; деградируем без него
    rag = None

try:
    from .colortype import analyze_colortype  # измерительный подтип (контраст важнее таблицы)
except Exception:  # noqa: BLE001
    analyze_colortype = None

_SEASONS = ("spring", "summer", "autumn", "winter")
_CONTRAST_SUBTYPE = {"high": "contrast", "medium": "natural", "low": "light"}


def refine_colortype_subtype(diagnosis: dict, photo_path: str) -> dict:
    """Уточнить ПОДТИП цветотипа измеренным контрастом «кожа↔волосы».

    По методу photo-reading.md контраст важнее таблицы. Калибровка показала: подтон по
    пикселям ненадёжен (его оставляем LLM/Vision = СЕЗОН), а контраст устойчив → ставит ПОДТИП.
    Любая ошибка — возвращаем диагноз как есть, чтобы не ронять генерацию.
    """
    if analyze_colortype is None:
        return diagnosis
    ct = diagnosis.get("colortype")
    season = ct.split("_")[0] if isinstance(ct, str) and "_" in ct else ""
    if season not in _SEASONS:
        return diagnosis
    try:
        level = analyze_colortype(photo_path).measurements.get("contrast_level")
        sub = _CONTRAST_SUBTYPE.get(level)
        if not sub:
            return diagnosis
        diagnosis = dict(diagnosis)
        diagnosis["colortype"] = f"{season}_{sub}"
        diagnosis["colortype_subtype_source"] = "measured_contrast"
    except Exception:  # noqa: BLE001
        pass
    return diagnosis


def analyze_photos(image_paths, height_cm: int | None = None, mode: str | None = None) -> dict:
    """Шаг 1. Vision: фото клиентки → JSON (цветотип, контраст, палитра, фигура)."""
    system = load_system_prompt("vision-analyzer")
    content = [provider.image_block(p) for p in image_paths]
    if height_cm:
        content.append(provider.text_block(json.dumps({"height_cm": height_cm}, ensure_ascii=False)))
    return provider.chat_json(config.model_for("vision", mode), system, content, max_tokens=2048)


def diagnose(quiz_answers: dict, vision_result: dict, mode: str | None = None) -> dict:
    """Шаг 2. Диагностика: ответы квиза + выход vision → Формула стиля + Identity Gap.

    RAG: до запроса подмешиваем в промпт правила из авторской базы под ранние сигналы
    (цветотип/фигура/желаемое впечатление), а после — прикрепляем «сработавшие правила»
    по итоговому профилю (`retrieved_rules`) для блока объяснимости.
    """
    system = load_system_prompt("formula-diagnostic")
    vis = _vision_to_diagnostic_input(vision_result)
    payload = {**quiz_answers, **vis}

    pre_rules = _rag_retrieve(_pre_profile(quiz_answers, vis))
    if pre_rules:
        system += (
            "\n\n# РЕЛЕВАНТНЫЕ ПРАВИЛА БАЗЫ ЗНАНИЙ (RAG)\n"
            "Опирайся на них при диагностике; при конфликте методология промпта выше.\n\n"
            + rag.rules_block(pre_rules)
        )

    result = provider.chat_json(
        config.model_for("text", mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )

    final_rules = _rag_retrieve(_diag_to_profile(result)) or pre_rules
    if final_rules:
        result["retrieved_rules"] = rag.cited_rules(final_rules)
    return result


def _rag_retrieve(profile: dict, k: int = 6) -> list:
    """RAG-поиск с мягким фолбэком: если индекс/библиотека недоступны — пусто."""
    try:
        return rag.retrieve(profile, k=k)
    except Exception:  # noqa: BLE001 — RAG не должен ронять диагностику
        return []


def _pre_profile(quiz: dict, vis: dict) -> dict:
    """Ранние сигналы до диагностики: цветотип (vision/квиз), фигура (самооценка), запрос."""
    return {
        "colortype": vis.get("colortype") or quiz.get("colortype_known"),
        "figure_type": vis.get("figure_type")
        or (quiz.get("physical") or {}).get("figure_type_self_assessed"),
        "want_traits_top3": quiz.get("want_traits_top3"),
        "style_formula": "",
    }


def _diag_to_profile(d: dict) -> dict:
    """Итоговый профиль диагностики → вход rag.retrieve (для объяснимости)."""
    return {
        "colortype": d.get("colortype"),
        "figure_type": d.get("figure_type"),
        "base_style": d.get("base_style"),
        "primary_substyle": d.get("primary_substyle"),
        "secondary_substyle": d.get("secondary_substyle"),
        "style_formula": d.get("style_formula"),
        "want_traits_top3": d.get("want_traits_top3"),
        "semantic_field_distribution": d.get("semantic_field_distribution"),
    }


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


_DIRECTIONS_SYSTEM = """Ты — AI-стилист Sense Style, работаешь по психологии моды (Self-Discrepancy \
Theory, enclothed cognition). На вход — Формула стиля клиентки (результат диагностики). \
Верни РОВНО два направления образа, оба служат её Формуле и желаемому впечатлению \
(want_traits), но различаются по интенсивности: первое — мягче и безопаснее, второе — \
собраннее и сильнее по характеру. Это не разные стили, а два прочтения одной Формулы.

ВАЖНО — образы должны быть ВИЗУАЛЬНО РАЗНЫМИ по составу одежды, а не двумя почти \
одинаковыми «брюки + жакет». Один образ строй вокруг ОДНОЙ базы, второй — вокруг ДРУГОЙ: \
например, платье; костюм с юбкой и пиджаком; брюки с блейзером; трикотажный комплект; \
рубашка с юбкой-миди. Выбирай базы, уместные под figure_type и Формулу, но между собой \
два образа должны явно отличаться силуэтом (где-то платье/юбка, где-то брюки), чтобы на \
фото это были два разных образа, а не один и тот же костюм в двух ракурсах.

Если в данных есть season/season_guidance — образы СТРОГО под этот сезон: вес и тип ткани, \
наличие/отсутствие верхней одежды, обувь, многослойность. Летом — без пальто; зимой — тёплый слой.

Тон: тёплый, на «ты», без восклицательных знаков, без эмодзи, без пустых усилителей \
(«потрясающе», «вау», «магия»). Объясняй через психологию впечатления, не через тренд. \
Палитра — из visual_formula, без табу-цветов из stop_list. Силуэты — под figure_type.

Верни ТОЛЬКО валидный JSON:
{
  "directions": [
    {
      "name": "<короткое название направления на русском, 1-3 слова>",
      "fits_if": "<«подходит, если хочется …» — 1 предложение через впечатление>",
      "items": ["<вещь1>", "<вещь2>", "<вещь3>", "<вещь4>", "<вещь5>"],
      "image_generation_prompt": "<английский промпт ОДЕЖДЫ и СЦЕНЫ образа: \
ЯВНО назови базу образа (dress / skirt+jacket / trousers+blazer / knit set), и она должна \
ОТЛИЧАТЬСЯ от базы второго образа; конкретные вещи, палитра, силуэт под фигуру, \
фотостиль/свет под характер направления. БЕЗ описания лица/личности — берётся с фото. ~60 слов>"
    },
    { … второе направление … }
  ]
}"""


SEASON_RU = {"spring": "весна", "summer": "лето", "autumn": "осень", "winter": "зима"}
_SEASON_HINT = {
    "spring": "весна — лёгкие слои, тренчи, рубашки, светлые ткани, прохладное утро",
    "summer": "лето — лёгкие ткани (лён, хлопок, шёлк), открытые силуэты, сандалии, без верхней одежды",
    "autumn": "осень — многослойность, трикотаж, жакеты, пальто, плотные ткани, ботинки",
    "winter": "зима — тёплый слой (пальто, шерсть, кашемир), закрытые силуэты, сапоги",
}


def generate_directions(diagnosis: dict, quiz: dict | None = None,
                        season: str | None = None, mode: str | None = None) -> list[dict]:
    """2 именованных направления образа из Формулы стиля (для экрана результата квиза).

    Один дешёвый текстовый вызов, grounded в диагностике и tone of voice. Каждое
    направление: name, fits_if, items[], image_generation_prompt (под рендер на клиентке).
    season — spring|summer|autumn|winter: образы собираются под этот сезон.
    """
    vf = diagnosis.get("visual_formula") or {}
    payload = {
        "style_formula": diagnosis.get("style_formula"),
        "base_style": diagnosis.get("base_style"),
        "primary_substyle": diagnosis.get("primary_substyle"),
        "secondary_substyle": diagnosis.get("secondary_substyle"),
        "accent_note": diagnosis.get("accent_note"),
        "figure_type": diagnosis.get("figure_type"),
        "colortype": diagnosis.get("colortype"),
        "palette": vf.get("palette"),
        "silhouettes": vf.get("silhouettes"),
        "stop_list": vf.get("stop_list"),
        "want_traits_top3": (quiz or {}).get("want_traits_top3"),
    }
    if season in _SEASON_HINT:
        payload["season"] = SEASON_RU[season]
        payload["season_guidance"] = _SEASON_HINT[season]
    result = provider.chat_json(
        config.model_for("text", mode), _DIRECTIONS_SYSTEM,
        json.dumps(payload, ensure_ascii=False), max_tokens=2048,
    )
    return (result.get("directions") or [])[:2]


_PALETTE_SYSTEM = """Ты — колорист Sense Style. На вход — цветотип и тональные параметры клиентки. \
Собери РАСШИРЕННУЮ персональную палитру СТРОГО под её цветотип: ровно 30 цветов, плюс стоп-цвета.

Правила:
- 30 цветов делятся: ~10 базовых/нейтралей, ~14 основных, ~6 акцентных.
- Каждый цвет — реальный, носибельный оттенок под её подтон/светлоту/контраст, с корректным hex.
- НЕ включай табу-цвета и цвета вне её цветотипа — они идут в stop_colors с короткой причиной.
- Названия по-русски, тёплым языком (например «тёплый графит», «пыльная роза», «сливочный»).

Верни ТОЛЬКО валидный JSON:
{
  "palette": [
    {"name": "<рус. имя>", "hex": "#RRGGBB", "group": "<base | main | accent>"}
    // ровно 30 элементов
  ],
  "stop_colors": [
    {"name": "<рус. имя>", "hex": "#RRGGBB", "why": "<почему гасит, 3-6 слов>"}
    // 4-6 элементов
  ]
}"""


def generate_card_palette(diagnosis: dict, mode: str | None = None) -> dict:
    """Палитра 30 цветов + стоп-цвета под цветотип (для продукта «Карта стиля»).

    Один текстовый вызов, grounded в цветотипе/тоне. Возвращает {palette[], stop_colors[]}.
    """
    vf = diagnosis.get("visual_formula") or {}
    payload = {
        "colortype": diagnosis.get("colortype"),
        "tonal_characteristics": diagnosis.get("tonal_characteristics"),
        "base_palette": vf.get("palette"),
        "stop_list": vf.get("stop_list"),
    }
    # max_tokens большой: pro («думающая») тратит часть на reasoning — при низком лимите
    # обрезает JSON (вернёт <30 цветов). 8000 хватает и pro, и flash.
    return provider.chat_json(
        config.model_for("text", mode), _PALETTE_SYSTEM,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )


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


def generate_shopping_list(diagnosis: dict, capsule: dict, price_segment: str = "middle",
                           mode: str = "teaser", text_mode: str | None = None) -> dict:
    """Шаг 4. Шоп-лист + бюджет: по капсуле подбирает бренды/запросы под бюджет и фигуру.

    Системный промпт shopping-list требует подклеенную brand-matrix. На выходе —
    shopping_items (с брендами и поисковыми запросами) и budget_estimate {min, max}.
    """
    system = (
        load_system_prompt("shopping-list")
        + "\n\n# БАЗА ЗНАНИЙ (brand-matrix)\n\n"
        + load_reference("reference/shopping/brand-matrix.md")
    )
    cap = capsule.get("capsule") or {}
    dist = diagnosis.get("semantic_field_distribution") or {}
    style_fields = [k for k, v in sorted(dist.items(), key=lambda kv: kv[1], reverse=True) if v > 0][:2]
    payload = {
        "capsule": cap.get("items") or [],
        "price_segment": price_segment,
        "style_fields": style_fields or [diagnosis.get("base_style")],
        "palette": (diagnosis.get("visual_formula") or {}).get("palette"),
        "figure_type": diagnosis.get("figure_type"),
        "mode": mode,
    }
    result = provider.chat_json(
        config.model_for("text", text_mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=3072,
    )
    # обогащаем deep-link в поиск маркетплейсов (без сбора данных — просто search-URL)
    for item in result.get("shopping_items") or []:
        item["links"] = marketplace_links(item.get("search_query", ""))
    return result


def marketplace_links(query: str) -> dict:
    """Готовые ссылки на поиск по запросу (без скрапинга/API — только search-URL)."""
    q = quote_plus(query or "")
    return {
        "wildberries": f"https://www.wildberries.ru/catalog/0/search.aspx?search={q}",
        "lamoda": f"https://www.lamoda.ru/catalogsearch/result/?q={q}",
        "ozon": f"https://www.ozon.ru/search/?text={q}",
    }


def evaluate_garment(garment_photo: str, diagnosis: dict, mode: str | None = None) -> dict:
    """«Брать / не брать»: фото вещи + Формула стиля → вердикт по методологии.

    Verdict: take | replace | skip + объяснение и чем заменить. Для кабинета/гардероба.
    """
    system = load_system_prompt("garment-check")
    content = [
        provider.text_block("Фото вещи для оценки:"),
        provider.image_block(garment_photo),
        provider.text_block("Профиль клиентки:\n" + json.dumps(_garment_input(diagnosis), ensure_ascii=False)),
    ]
    return provider.chat_json(config.model_for("vision", mode), system, content, max_tokens=700)


def _garment_input(d: dict) -> dict:
    # экспресс-анкета (линии/посадка/ДНК/анти-гардероб) + при наличии полная диагностика
    keys = [
        "silhouette_lines", "fit_focus", "fit_challenges", "style_dna", "impression",
        "dealbreakers", "style_formula", "base_style", "figure_type", "colortype",
        "visual_formula",
    ]
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def render_look_on_client(client_photo: str, look_prompt: str, ref_image: str | None = None) -> str:
    """Identity-preserving рендер: фото клиентки + промпт образа → она в этом образе.

    Gemini 3 Pro image-to-image: держит лицо/волосы/фигуру, меняет только одежду.
    (GPT image отпал — OpenAI отказывается воссоздавать реальные лица.)
    look_prompt — это look-generator.looks[].image_generation_prompt. Возвращает data-URL.
    """
    instruction = (
        "Photo editing task: dress the SAME real woman from the reference photo in a new outfit. "
        "Preserve her identity EXACTLY — she must be instantly recognizable as the same person:\n"
        "- Face: keep the same face shape, eyes, nose, lips, eyebrows, skin tone and complexion, and age. "
        "Do NOT beautify, slim the face, or alter any feature.\n"
        "- Hair: keep the same colour, length and texture.\n"
        "- Body: keep the same height, build, weight and body proportions (figure type). "
        "Do NOT slim, lengthen, or idealise her body — keep her real silhouette.\n"
        "Change ONLY her clothing and the background. "
        "Place her in a new location that fits the outfit's setting (do not reuse the reference background). "
        "Outfit and scene: " + look_prompt
        + " Full-body head to toe, photorealistic, natural light, vertical 3:4 ratio."
    )
    model = config.MODELS["image"]["dressing"]
    return provider.generate_image(instruction, model=model, ref_images=[ref_image or client_photo])[0]


def render_capsule_on_client(client_photo: str, look_prompts: list[str]) -> list[str]:
    """Все образы капсулы на клиентке (один человек во всех образах). Список data-URL.

    Каждый образ берёт исходное фото как референс личности — так лицо/фигура держатся.
    """
    return [render_look_on_client(client_photo, p) for p in look_prompts]


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
