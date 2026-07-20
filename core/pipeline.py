"""Сквозной пайплайн: vision-анализ → диагностика Формулы стиля → (капсула).

Шаги 1-2 (vision + диагностика) реализованы. Шаг 3 (look-generator + генерация
образов через Seedream) подключается в Фазе 2 — см. plans/2026-06-25-mvp-vertical-slice.md.
"""
from __future__ import annotations
import json
import re
import sys
from urllib.parse import quote_plus

from . import config, provider
from .canon import canon_rule, enforce_substyles
from .figure_rules import fit_rules_prompt
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
    ct = diagnosis.get("colortype")
    season = ct.split("_")[0] if isinstance(ct, str) and "_" in ct else ""
    if season not in _SEASONS:
        return diagnosis
    # 1) измеренный контраст «кожа↔волосы» (надёжнее);
    level, source = None, "measured_contrast"
    if analyze_colortype is not None:
        try:
            level = analyze_colortype(photo_path).measurements.get("contrast_level")
        except Exception:  # noqa: BLE001
            level = None
    # 2) фолбэк: контраст из диагностики (LLM) — чтобы ПОДТИП не противоречил контрасту
    #    (раньше при неудачном замере оставался подтип LLM: «высокий контраст» + «светлая»).
    if level not in _CONTRAST_SUBTYPE:
        level = (diagnosis.get("tonal_characteristics") or {}).get("contrast")
        source = "diagnosis_contrast"
    sub = _CONTRAST_SUBTYPE.get(level)
    if not sub:
        return diagnosis
    diagnosis = dict(diagnosis)
    diagnosis["colortype"] = f"{season}_{sub}"
    diagnosis["colortype_subtype_source"] = source
    return diagnosis


def analyze_photos(image_paths, height_cm: int | None = None, mode: str | None = None) -> dict:
    """Шаг 1. Vision: фото клиентки → JSON (цветотип, контраст, палитра, фигура)."""
    system = load_system_prompt("vision-analyzer")
    content = [provider.image_block(p) for p in image_paths]
    if height_cm:
        content.append(provider.text_block(json.dumps({"height_cm": height_cm}, ensure_ascii=False)))
    return provider.chat_json(config.model_for("vision", mode), system, content, max_tokens=2048)


_GAP_FIELDS = ("natural", "romance", "drama", "classic")
_EXPRESSION_STEPS = (0, 15, 25)   # надбавка за невыраженность образа (formula-diagnostic.md)


def _recompute_gap(diag: dict) -> dict:
    """Пересчитать Identity Gap в коде из распределений, которые дала модель.

    Разделяем роли: модель КЛАССИФИЦИРУЕТ черты по семантическим полям (языковая задача — её
    сильная сторона), а арифметику считаем мы. Раньше LLM сама складывала, делила и округляла —
    а Gap это вся метрика продукта: на нём стоит «измеримая трансформация», слайд для жюри и
    обещание клиентке. Держать основание измерительного инструмента на вероятностной модели
    нельзя, даже если она обычно не ошибается.

    Формула (architecture/prompts/formula-diagnostic.md, v1.1 field-aware):
        field_gap = Σ|want − now| / 2      по 4 полям
        gap       = min(99, round(field_gap + expression_gap))

    Расхождение с числом модели пишем в `gap_llm_mismatch` — это метрика её надёжности, а не
    просто отладка. Если распределений нет (старый ответ/сбой) — оставляем как было.
    """
    now = diag.get("now_field_distribution")
    want = diag.get("semantic_field_distribution")
    if not isinstance(now, dict) or not isinstance(want, dict):
        return diag

    def _num(d: dict, k: str) -> float:
        v = d.get(k)
        return float(v) if isinstance(v, (int, float)) else 0.0

    # распределения должны быть долями по 100; кривую сумму не «чиним» молча — она сама сигнал
    now_sum = sum(_num(now, k) for k in _GAP_FIELDS)
    want_sum = sum(_num(want, k) for k in _GAP_FIELDS)
    if not (90 <= now_sum <= 110 and 90 <= want_sum <= 110):
        diag["gap_distribution_broken"] = {"now_sum": round(now_sum), "want_sum": round(want_sum)}
        return diag

    field_gap = sum(abs(_num(want, k) - _num(now, k)) for k in _GAP_FIELDS) / 2
    breakdown = diag.get("gap_breakdown") if isinstance(diag.get("gap_breakdown"), dict) else {}
    expression = breakdown.get("expression_gap")
    if expression not in _EXPRESSION_STEPS:      # модель обязана выбрать одну из трёх ступеней
        expression = 0
    gap = min(99, round(field_gap + expression))

    llm_gap = diag.get("gap_percentage")
    if isinstance(llm_gap, (int, float)) and round(llm_gap) != gap:
        diag["gap_llm_mismatch"] = {"llm": round(llm_gap), "computed": gap,
                                    "field_gap": round(field_gap, 1), "expression_gap": expression}
        # в лог сервера: иначе мы никогда не узнаем, врёт ли модель в арифметике и как часто
        print(f"[gap] модель посчитала {round(llm_gap)}, код — {gap} "
              f"(field_gap={round(field_gap, 1)} + expression={expression}). Берём расчёт кода.",
              file=sys.stderr)
    diag["gap_percentage"] = gap
    diag["gap_breakdown"] = {"field_gap": round(field_gap, 1), "expression_gap": expression}
    return diag


def diagnose(quiz_answers: dict, vision_result: dict, mode: str | None = None) -> dict:
    """Шаг 2. Диагностика: ответы квиза + выход vision → Формула стиля + Identity Gap.

    RAG: до запроса подмешиваем в промпт правила из авторской базы под ранние сигналы
    (цветотип/фигура/желаемое впечатление), а после — прикрепляем «сработавшие правила»
    по итоговому профилю (`retrieved_rules`) для блока объяснимости.
    """
    system = load_system_prompt("formula-diagnostic") + "\n\n" + canon_rule()
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

    result = _recompute_gap(result)   # арифметику Gap не доверяем модели — считаем сами
    result = enforce_substyles(result)  # подстиль — только из 25, а не выдуманный ярлык

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

НАЗВАНИЕ НАПРАВЛЕНИЯ — НЕ НОВЫЙ СТИЛЬ. В методе есть 4 стиля и 25 подстилей, других не \
существует. Не придумывай названий стилей и не склеивай их в новые ярлыки: «Нежная \
Реформаторская», «Структурный Романтизм», «Мягкий Авангард» — так нельзя. Клиентка только \
что получила свою Формулу, и чужой ярлык рядом с ней читается как вторая, другая диагностика.

Название описывает РЕГИСТР прочтения её Формулы, а не стиль: «Мягкая версия», «Собранная \
версия», «Тихое прочтение», «Сильное прочтение», «Спокойный регистр», «Уверенный регистр». \
Допустимо назвать направление её собственным подстилем из Формулы, если он там есть. \
Всё остальное — выдумка, которой в методе нет.

Верни ТОЛЬКО валидный JSON:
{
  "directions": [
    {
      "name": "<регистр прочтения её Формулы, 1-3 слова: «Мягкая версия» / «Собранная версия» \
и подобное. НЕ выдуманное название стиля>",
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
        config.model_for("text", mode), _DIRECTIONS_SYSTEM + "\n\n" + canon_rule(),
        json.dumps(payload, ensure_ascii=False), max_tokens=2048,
    )
    return _canonical_direction_names(
        (result.get("directions") or [])[:2], diagnosis)


# Слова методологии: из них собраны названия 4 стилей и 25 подстилей. Если модель склеила из них
# новый ярлык («Структурный Романтизм»), это выдуманный стиль, которого в методе нет.
_STYLE_WORDS = (
    "класс", "драм", "романт", "натурал", "авангард", "минимал", "casual", "кэжуал",
    "реформат", "готич", "бохо", "гламур", "винтаж", "милитари", "сафари", "спорт",
    "преппи", "этно", "фольк", "рустик", "денди", "гарсон", "вамп", "леди",
)
_REGISTER_OK = ("верси", "прочтен", "регистр", "вариант", "мягк", "собран", "тих", "сильн",
                "спокойн", "уверенн")


def _canonical_direction_names(directions: list[dict], diagnosis: dict) -> list[dict]:
    """Страховка от выдуманных названий стилей в направлениях.

    Направление — это РЕГИСТР прочтения Формулы, а не новый стиль: клиентка только что получила
    свою Формулу, и ярлык вроде «Нежная Реформаторская» рядом с ней читается как вторая, другая
    диагностика. Промпт это запрещает, но модель может сдрейфовать — здесь чиним молча.

    Название из подстилей самой клиентки не трогаем: это её Формула, а не выдумка.
    """
    own = {str(diagnosis.get(k) or "").strip().lower()
           for k in ("primary_substyle", "secondary_substyle", "style_formula")}
    own.discard("")
    fallback = ["Мягкая версия", "Собранная версия"]
    out = []
    for i, d in enumerate(directions):
        d = dict(d)
        name = str(d.get("name") or "").strip()
        low = name.lower()
        invented = (any(w in low for w in _STYLE_WORDS)
                    and not any(w in low for w in _REGISTER_OK)
                    and not any(low in o or o in low for o in own))
        if not name or invented:
            d["name"] = fallback[i] if i < len(fallback) else "Ещё одно прочтение"
        out.append(d)
    return out


_PALETTE_SYSTEM = """Ты — колорист Sense Style. На вход — цветотип и тональные параметры клиентки. \
Собери персональную палитру СТРОГО под её цветотип: ровно 18 цветов, плюс стоп-цвета.

Правила:
- 18 цветов делятся: 6 базовых/нейтралей, 8 основных, 4 акцентных.
- Меньше — лучше: клиентка должна ЗАПОМНИТЬ свою палитру и узнавать её в магазине. Тридцать
  оттенков не запоминаются и превращают палитру в справочник. Оставляй только те, что она
  реально будет носить.
- Каждый цвет — реальный, носибельный оттенок под её подтон/светлоту/контраст, с корректным hex.
- ЗАПРЕЩЕНЫ чистые спектральные и неоновые цвета: #0000FF, #FF00FF, #00FFFF, #FFFF00, #00FF00 \
и любые кислотные. Таких тканей не существует. Даже яркий цвет у Зимы — это плотный текстильный \
оттенок (королевский синий #2B4C9B, фуксия #A83A6B, изумруд #0B6E4F), а не спектр из палитры Paint.
- НЕ включай табу-цвета и цвета вне её цветотипа — они идут в stop_colors с короткой причиной.
- Названия по-русски, тёплым языком (например «тёплый графит», «пыльная роза», «сливочный»).

Верни ТОЛЬКО валидный JSON:
{
  "palette": [
    {"name": "<рус. имя>", "hex": "#RRGGBB", "group": "<base | main | accent>"}
    // ровно 18 элементов
  ],
  "stop_colors": [
    {"name": "<рус. имя>", "hex": "#RRGGBB", "why": "<почему гасит, 3-6 слов>"}
    // 4-6 элементов
  ]
}"""


def _wearable_hex(hex_str: str) -> str:
    """Приглушить спектральный/неоновый цвет до носибельного текстильного оттенка, сохранив тон.

    Модель, добивая палитру до 30 цветов, скатывается в чистый спектр (#0000FF, #FF00FF, #00FFFF,
    лайм, кислотный жёлтый) — таких тканей не бывает, и палитра выглядит как из Paint. Промпт это
    запрещает, но не гарантирует, поэтому режем на выходе: срезаем экстремальную насыщенность
    и яркость. Тон (hue) не трогаем — цветотип не уезжает.
    """
    import colorsys
    s = (hex_str or "").strip().lstrip("#")
    if len(s) != 6:
        return hex_str
    try:
        r, g, b = (int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return hex_str
    h, sat, val = colorsys.rgb_to_hsv(r, g, b)
    # кислотность = высокая насыщенность ВМЕСТЕ со светлотой. Тёмные плотные цвета (изумруд, бордо,
    # бутылочный) насыщены не меньше, но носибельны — их не трогаем.
    if sat > 0.85 and val > 0.6:
        sat = 0.75
    if val > 0.95 and sat > 0.55:   # «светится» → приглушаем светлоту
        val = 0.88
    r, g, b = colorsys.hsv_to_rgb(h, sat, val)
    return "#{:02X}{:02X}{:02X}".format(round(r * 255), round(g * 255), round(b * 255))


def generate_card_palette(diagnosis: dict, mode: str | None = None) -> dict:
    """Палитра 30 цветов + стоп-цвета под цветотип (для продукта «Карта стиля»).

    Один текстовый вызов, grounded в цветотипе/тоне. Возвращает {palette[], stop_colors[]}.
    Спектральные hex приглушаются до носибельных (_wearable_hex).
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
    result = provider.chat_json(
        config.model_for("text", mode), _PALETTE_SYSTEM,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )
    for key in ("palette", "stop_colors"):
        for c in (result.get(key) or []):
            if isinstance(c, dict) and c.get("hex"):
                c["hex"] = _wearable_hex(c["hex"])
    return result


_STYLE_RU = {"classic": "Классика", "drama": "Драма", "romantic": "Романтика", "natural": "Натуральный"}

# Правила качества капсулы: против монотонности, «мягкого уюта по умолчанию» и приглушённости.
# Появились из разбора реального промаха (клиентка: «не моё» — все образы одинаковые, casual, без акцента).
_CAPSULE_QUALITY_RULES = (
    "1. РАЗНООБРАЗИЕ: каждый образ — самостоятельная комбинация. НЕ повторяй одни и те же 2–3 вещи "
    "во всех образах. Варьируй силуэт, длину, верхний слой и обувь между образами. "
    "Обувь и сумка тоже варьируются: одна пара обуви и одна сумка на все шесть образов — ошибка, "
    "капсула должна давать минимум две пары обуви и две сумки разного регистра.\n"
    "1a. ЦВЕТ В НАЗВАНИИ: в поле name каждой вещи обязательно указывай её цвет словами из палитры "
    "клиентки («Прямые брюки со стрелкой, глубокий шоколад»). Без цвета вещь невозможно ни купить, "
    "ни собрать с остальными — название без цвета считается неполным.\n"
    "2. ДИАПАЗОН ФОРМАЛЬНОСТИ по сценарию (не своди всё к расслабленному casual):\n"
    "   - «работа», «деловая встреча» → СТРУКТУРНЫЙ собранный образ: жакет/костюм/рубашка/юбка-карандаш/"
    "прямые брюки со стрелкой, аккуратная обувь (лодочки, лоферы). НЕ крупный трикотаж-уют, НЕ джоггеры/худи.\n"
    "   - «событие и выход», «свидание» → нарядный акцент, более выразительный силуэт или ткань.\n"
    "   - «повседневное», «путешествие» → расслабленнее, но всё равно собранно.\n"
    "3. АКЦЕНТ: в каждом образе — один насыщенный цветовой акцент из палитры (сумка/обувь/верх/аксессуар), "
    "если он не в стоп-листе. Образ не должен быть целиком приглушённым/бежево-серым.\n"
    "4. ХАРАКТЕР ФОРМУЛЫ: сохраняй регистр Формулы стиля клиентки. Если в ней есть Драма или Классика — "
    "держи структуру, чёткие линии, собранность; НЕ сводить образ к мягкому уюту по умолчанию.\n"
    "5. МЕТАЛЛ по цветотипу: тёплый → золото/латунь; холодный → серебро/белое золото. Украшения и фурнитура — в тон.\n"
    "6. АКСЕССУАРЫ и завершённость: добавляй завершающие детали (пояс-акцент, украшение, сумка), "
    "чтобы образ выглядел собранным стилистом, а не «свитер + брюки».\n"
    "7. СТРУКТУРА КАПСУЛЫ (канон «Алгоритмы имиджа»): верхов БОЛЬШЕ, чем низов (2–3 низа на 4–5 верхов) — "
    "капсула богатеет за счёт верхов. Низы и верхи максимально РАЗНОПЛАНОВЫЕ по крою, фактуре, длине, "
    "регистру: два похожих низа — потерянный слот. Каждая вещь миксуется минимум с 3 другими; вещь, "
    "которая ни с чем не встаёт («вещь-сиротка»), в капсулу не входит.\n"
    "8. БАЗА vs ТРЕНД: 70–80% капсулы — база (простой крой, воздух: прямой / лёгкий оверсайз / "
    "полуприлегающий; посадка высокая или средняя), 20–30% — 1–2 трендовых акцента. Верхняя одежда — "
    "структурная, формодержащая, с воздухом под многослойность; длина ниже самой широкой части бедра "
    "или миди/макси. Талию в шубе и пуховике создаём поясом, а не кроем.\n"
    "9. НЕ ПРЕДЛАГАТЬ УСТАРЕВШЕЕ: рукав 3/4, длинные угги и дутики, рюкзаки и мини-сумки в городе, "
    "дешёвая меховая опушка, пуховик со встроенной талией, деним с потёртостями, стразами, бахромой "
    "и жёлтой/выбеленной варкой.\n"
    "10. БАЗА НЕ ЗНАЧИТ БЕЖЕВО-СЕРАЯ: бери 2–3 ярких цвета из палитры как основу, остальное — нейтрали. "
    "Капсула целиком в приглушённых тонах — брак."
)


_SUBSTYLES_CACHE: str | None = None


def _substyles_reference() -> str:
    """Раздел метода «25 уточняющих подстилей» — маркеры, эталон, прототипы каждого подстиля.

    Берём именно из sense-style-method.md: reference/style-typology — это МАППИНГ стилей курса на
    наши 4 поля, описаний вещей там нет. Тянем один раздел, а не файл целиком (34 КБ): промпт и так
    ~10 тыс. токенов, а генератору нужны подстили, не манифест и не теория.
    """
    global _SUBSTYLES_CACHE
    if _SUBSTYLES_CACHE is not None:
        return _SUBSTYLES_CACHE
    try:
        text = load_reference("sense-style-method.md")
    except FileNotFoundError:
        _SUBSTYLES_CACHE = ""
        return ""
    m = re.search(r"\n## 5\. 25 уточняющих подстилей\n", text)
    if not m:                      # метод переструктурировали — лучше отдать всё, чем ничего
        _SUBSTYLES_CACHE = text
        return text
    start = m.start()
    nxt = re.search(r"\n## 6\.", text[start:])
    _SUBSTYLES_CACHE = text[start:start + nxt.start()] if nxt else text[start:]
    return _SUBSTYLES_CACHE


def generate_capsule(diagnosis: dict, generation_request: dict, mode: str | None = None) -> dict:
    """Шаг 3. Капсула: Формула стиля + запрос → капсула вещей + образы с промптами для генерации.

    Системный промпт look-generator требует подклеенный целиком style-library (knowledge base).
    Каждый образ на выходе содержит image_generation_prompt — он пойдёт в Seedream (Фаза 2).
    """
    system = (
        canon_rule() + "\n\n"
        + load_system_prompt("look-generator")
        + "\n\n# БАЗА ЗНАНИЙ (style-library)\n\n"
        + load_knowledge("style-library")
        # Без описаний подстилей модель видит только ЯРЛЫК и наполняет его как умеет: реальный
        # случай (17.07.2026) — клиентке с формулой «Леди-лайк × Soft Classic» собран образ с
        # фланелевой рубашкой в клетку. В style-library леди-лайк есть лишь в таблице
        # соответствий («→ №17»), а что он значит (платья приталенного силуэта, миди, перчатки,
        # эстетика 50-60-х, Одри Хепберн) — только в методе. Ярлык без содержания = стиль мимо.
        + "\n\n# 25 ПОДСТИЛЕЙ — ЧТО КАЖДЫЙ ЗНАЧИТ (обязательно к соблюдению)\n\n"
        + _substyles_reference()
    )
    # грунтуем подбор явными правилами посадки под фигуру (размеры/силуэты считываются при подборе)
    fit_prompt = fit_rules_prompt(diagnosis.get("figure_type"))
    if fit_prompt:
        system += "\n\n# ПОСАДКА ПОД ФИГУРУ (обязательно к соблюдению)\n\n" + fit_prompt
    system += "\n\n# КАЧЕСТВО КАПСУЛЫ (обязательно к соблюдению)\n\n" + _CAPSULE_QUALITY_RULES
    # Палитра колориста — ДО сборки образов, иначе шаг колориста напрасен. Раньше сюда доезжала
    # только грубая палитра из диагностики (visual_formula.palette), а выверенная палитра Карты и
    # стоп-цвета не доезжали вовсе: клиентка видела в Карте одну палитру, а образы были собраны в
    # других цветах, вплоть до тех, что её гасят.
    pal = [p for p in (generation_request.get("palette") or []) if p.get("name")]
    stop = [p for p in (generation_request.get("stop_colors") or []) if p.get("name")]
    if pal:
        by_group = {}
        for p in pal:
            by_group.setdefault(p.get("group") or "прочие", []).append(p["name"])
        titles = {"base": "База и нейтрали", "main": "Основные", "accent": "Акценты"}
        lines = [f"- {titles.get(g, g)}: {', '.join(names)}" for g, names in by_group.items()]
        system += (
            "\n\n# ПАЛИТРА КЛИЕНТКИ (обязательно к соблюдению)\n"
            "Цвета вещей бери ТОЛЬКО из этой палитры — она выверена под её цветотип:\n"
            + "\n".join(lines)
            + "\nАкцент в образе — из группы «Акценты», один на образ. "
              "Базу и нейтрали используй как основу гардероба."
        )
    if stop:
        system += ("\n\n# СТОП-ЦВЕТА (не использовать никогда)\n"
                   "Эти цвета гасят клиентку, их не должно быть ни в одной вещи образа: "
                   + ", ".join(f"{p['name']}" + (f" ({p['why']})" if p.get("why") else "") for p in stop) + ".")
    # клиентка сама отметила любимые стили (визуальный выбор) → явный регистр образов
    want = generation_request.get("want_styles") or []
    if want:
        names = ", ".join(_STYLE_RU.get(c, c) for c in want)
        system += ("\n\n# ВЫБОР СТИЛЯ КЛИЕНТКОЙ\nОна отметила, что ей откликаются направления: "
                   + names + ". Строй образы в этом характере (в рамках её Формулы), не уводи в другой регистр.")
    payload = {
        "style_formula_result": _diagnosis_to_formula_result(diagnosis),
        "generation_request": generation_request,
    }
    return provider.chat_json(
        config.model_for("text", mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=8000,
    )


_STYLING_SYSTEM = (
    "Ты — стилист-методолог сервиса «Чувство стиля». Покажи капсульную логику наглядно: "
    "ОДНА базовая вещь — ДВА разных образа. Это демонстрация принципа «мало вещей — много образов».\n"
    "Дано: Формула клиентки, палитра, фигура, список вещей капсулы. Выбери ОДНУ универсальную "
    "базовую вещь ИЗ капсулы (брюки, юбка, тренч, рубашка, джемпер) и собери на ней ДВА контрастных "
    "по сценарию образа — например, деловой и расслабленный выходной. Вещи бери из палитры/капсулы, "
    "носибельно для реальной жизни, под фигуру.\n"
    "Описания — на русском, на «ты», через психологию запроса, без восклицательных знаков и штампов.\n"
    "image_generation_prompt — на АНГЛИЙСКОМ, конкретный (вещи, цвета, силуэт, обувь, сценарий), "
    "для фотореалистичного рендера в полный рост. Только АКТУАЛЬНЫЙ крой: рукав полноразмерный "
    "(не 3/4), актуальные длины, структурная верхняя одежда; без устаревшего (скинни с грубыми "
    "ботинками, дешёвый мех, мини-рюкзак).\n"
    "Верни СТРОГО JSON: {\"base_item\":\"<вещь>\", \"idea\":\"<1 фраза: как одна вещь даёт два образа>\", "
    "\"looks\":[{\"title\":\"<коротко>\",\"scenario\":\"<сценарий>\",\"items\":[\"<вещь>\"],"
    "\"description\":\"<2-3 фразы>\",\"image_generation_prompt\":\"<english>\"}]} — РОВНО 2 образа в looks."
)


def generate_styling_pair(diagnosis: dict, capsule_items: list | None, mode: str | None = None) -> dict:
    """Стилизация: одна базовая вещь → два образа (капсульная логика). Для Карты стиля.

    Лёгкий focused-вызов (flash): надёжнее, чем тащить это в большой look-generator.
    Возвращает {base_item, idea, looks:[{…, image_generation_prompt} x2]} для рендера на клиентке.
    """
    names = [it.get("name") for it in (capsule_items or []) if isinstance(it, dict) and it.get("name")]
    payload = {
        "style_formula_result": _diagnosis_to_formula_result(diagnosis),
        "palette": (diagnosis.get("visual_formula") or {}).get("palette"),
        "figure_type": diagnosis.get("figure_type"),
        "capsule_items": names[:14],
    }
    return provider.chat_json(
        config.model_for("text", mode), _STYLING_SYSTEM,
        json.dumps(payload, ensure_ascii=False), max_tokens=2200,
    )


_PORTRAIT_SYSTEM = (
    "Ты — психолог-стилист сервиса «Чувство стиля». По уровням черт Big Five (научная модель личности) "
    "напиши ЖИВОЙ персональный портрет клиентки и связь с её стилем. БЕЗ ярлыков-архетипов "
    "(не «ты — Королева»), без процентов и терминов Big Five — только человеческим языком про НЕЁ.\n"
    "Тон: тёплый, на «ты», через психологию, без восклицаний и штампов (как Ксения Колупаева).\n"
    "Свяжи личность с одеждой: как её натура хочет считываться и что это значит для образа "
    "(силуэт, цвет, степень драмы/спокойствия, формальность). Опирайся на принцип «ценности → стиль».\n"
    "Верни СТРОГО JSON: {\"portrait\": \"<2-4 фразы про неё, человечно>\", "
    "\"style_implications\": [\"<вывод для стиля>\", \"<вывод>\", \"<вывод>\"]} — 3 вывода."
)

# Человеческие ярлыки уровней (для подсказки модели, не показываются клиентке)
_TRAIT_RU = {
    "O": ("открытость новому, любознательность, тяга к эстетике", "практичность, опора на привычное"),
    "C": ("организованность, дисциплина, доведение до конца", "спонтанность, гибкость, лёгкость"),
    "E": ("энергия от людей, инициатива, заметность", "сдержанность, глубина, комфорт в тишине"),
    "A": ("забота о других, тепло, уступчивость", "автономность, прямота, опора на себя"),
    "S": ("спокойствие, устойчивость, ровный фон", "чувствительность, эмоциональная тонкость"),
}


def generate_personality_portrait(traits: dict, diagnosis: dict, mode: str | None = None) -> dict:
    """Big Five → живой персональный портрет + выводы для стиля (без архетипов-ярлыков).

    traits: {O/C/E/A/S: 'high'|'mid'|'low'}. Возвращает {portrait, style_implications:[…]}.
    """
    hints = []
    for k, (hi, lo) in _TRAIT_RU.items():
        lvl = (traits or {}).get(k)
        if lvl == "high":
            hints.append(hi)
        elif lvl == "low":
            hints.append(lo)
    payload = {
        "trait_levels": traits,
        "human_hints": hints,  # к чему склонна (человеческим языком)
        "style_formula": diagnosis.get("style_formula"),
        "want_traits": (diagnosis.get("semantic_field_distribution") or {}),
    }
    return provider.chat_json(
        config.model_for("text", mode), _PORTRAIT_SYSTEM,
        json.dumps(payload, ensure_ascii=False), max_tokens=1200,
    )


def refine_substyle(diagnosis: dict, deep_intake: dict, mode: str | None = None) -> dict:
    """Шаг 4 метода: уточнить ПОДСТИЛЬ (из 25) по ПСИХОТИПУ (Big Five) + визуальному выбору
    стилей + кругу жизни. Психотип — движок глубины: превращает базовый стиль (4) в подстиль (25).

    Возвращает {base_style, primary_substyle, secondary_substyle, accent_note, style_formula,
    substyle_rationale, requires_stylist_validation}. Без психотипа (big5) возвращает {} —
    подстиль остаётся тем, что дала диагностика (базовый уровень).
    """
    big5 = (deep_intake or {}).get("big5") or {}
    if not big5:
        return {}
    hints = []
    for k, (hi, lo) in _TRAIT_RU.items():
        lvl = big5.get(k)
        if lvl == "high":
            hints.append(hi)
        elif lvl == "low":
            hints.append(lo)
    system = (
        load_system_prompt("substyle-refine")
        + "\n\n" + canon_rule()
        + "\n\n# БАЗА ЗНАНИЙ (25 подстилей)\n\n"
        + load_reference("reference/style-typology/style-typology.md")
    )
    payload = {
        "base_style": diagnosis.get("base_style"),
        "current_primary_substyle": diagnosis.get("primary_substyle"),
        "current_secondary_substyle": diagnosis.get("secondary_substyle"),
        "current_accent_note": diagnosis.get("accent_note"),
        "semantic_field_distribution": diagnosis.get("semantic_field_distribution"),
        "want_traits_top3": diagnosis.get("want_traits_top3"),
        "psychotype_levels": big5,
        "psychotype_hints": hints,  # к чему склонна её натура (человеческим языком)
        "want_styles": (deep_intake or {}).get("want_styles"),  # визуальный выбор стилей
        "lifecircle": (deep_intake or {}).get("lifecircle"),
    }
    return provider.chat_json(
        config.model_for("text", mode), system,
        json.dumps(payload, ensure_ascii=False), max_tokens=1500,
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


# Фото-финиш (анти-пластик, по курсу ART AI — см. [[art-ai-nonplastic-photos]] и
# scripts/gen_editorial_image.py). Даёт редакционную «плёночную» реалистичность БЕЗ смены личности:
# все фразы усиливают «не бьютить, реальная кожа», что совпадает с задачей identity-preserving.
# Палитру НЕ фиксируем здесь — цвета приходят из промпта образа под цветотип клиентки.
_PHOTO_FINISH = (
    " Editorial fashion photograph shot on Kodak Portra 400 film, 85mm lens, shallow depth of field, "
    "authentic film grain, natural soft contrast, true-to-life colors. "
    # ВАЖНО: раньше здесь стояло «visible texture, pores and fine lines». В связке с плёнкой и
    # documentary-подачей модель ДОРИСОВЫВАЛА морщины, которых на референсе нет, — клиентка
    # получала себя старше, чем она есть. Текстуру кожи воспроизводим ПО РЕФЕРЕНСУ, а не добавляем.
    "Real unretouched skin with natural texture and pores exactly as in the reference photo, "
    "no beauty retouching, no skin smoothing, matte natural complexion, candid documentary feel. "
    "Not plastic, not waxy, not glossy, not airbrushed, not CGI, not a 3D render, not over-saturated. "
    "No text, no logos, no watermark."
)

# Правила капсулы в рендере (по канону «Алгоритмы имиджа», см. base-vs-trend.md) — страховка на случай,
# если image-модель «сдрейфует» в устаревший силуэт. Держим актуальный крой в самой картинке.
# Образы должны читаться как съёмка из модного журнала сезона, а не как карточка маркетплейса.
# Мы не копируем вещи магазинов — мы показываем СТИЛЬ, поэтому силуэт и режиссура кадра важнее
# «похожести на товар». Ориентир — актуальная editorial-мода 2026-2027: тихая роскошь, чистая
# линия, объём в правильных местах.
_LOOK_CANON = (
    " The outfit must read as current editorial fashion for the 2026-2027 season: "
    "elongated clean lines, considered proportions, quiet-luxury materials (wool, silk, cashmere, "
    "fine leather), tonal or restrained colour blocking, one deliberate accent — never busy. "
    "Modern silhouette cues: relaxed wide or straight trousers with proper break, elongated or "
    "softly structured tailoring, generous outerwear worn over slim layers, midi lengths, "
    "sculptural bag, refined footwear. Full-length sleeves (never 3/4). "
    "No dated styling: no skinny jeans with chunky boots, no cheap fur trim, no bulky mini-backpack, "
    "no logo-heavy fast-fashion pieces, no shiny synthetic fabrics."
)

# Конкретика сезона по показам SS26 и FW26/27 — см. architecture/trends-2026-2027.md (там же
# источники и правило применимости). Без названного списка модель опирается на смутное
# «что-то модное» и выдаёт вневременную базу, которая не читается как этот сезон.
# ОБНОВЛЯТЬ РАЗ В СЕЗОН вместе с тем файлом: промпт, застрявший на прошлом сезоне, хуже,
# чем отсутствие трендов вообще.
_TREND_CANON = (
    # Пропорция гардероба из курса: образ строится на базе, тренд — акцент, а не наполнение.
    " Build the look as roughly 70-80% long-lasting base and 20-30% current trend — the base is "
    "the load-bearing structure, the trend is the decor. "
    "Base that holds for years: straight or semi-fitted cut with air between body and fabric, "
    "high or mid rise, full-length sleeves. "
    "Current 2026-2027 season notes, used where they suit her formula: tailoring is fitted and "
    "structured again (narrow jackets, tapered trousers, defined waist, softly squared shoulders); "
    "archival new-luxury, 80s-90s power dressing, cold Helmut Lang minimalism, military and naval "
    "cues (officer details, double-breasted pea coats, duffle coats). "
    # Правило длин — прямая цитата методологии, модель иначе даёт «спорные» промежуточные длины.
    "Lengths: the wider the trousers the longer they run (full-length palazzo or wide leg); the "
    "narrower, the shorter (7/8 to the ankle bone). Skirts and dresses read as clear midi/midaxi or "
    "clear mini — never an ambiguous in-between length. "
    # Цвет: 70/30, с главным оттенком сезона.
    "Colour: about 70% neutral foundation (black, white, grey, beige, sand, milk, camel, navy, "
    "burgundy, olive, khaki, powder, denim) plus 2-3 accents. Season colours: hot chocolate as the "
    "key shade, cherry-burgundy, cranberry, umber, deep grown-up blue/green/purple, mustard, "
    "powder pink. Leather in ripe cherry, velvet, croc texture, fur collars, oversized collars, "
    "lace, checks, argyle, animal print. "
    # Стоп-лист. Конкретика важнее общих слов: без имён модель воспроизводит именно эти клише.
    "NEVER include these dated markers: 3/4 or rolled-up sleeves, skinny jeans, bleached or "
    "yellowed denim wash, rips, rhinestones, a built-in waist dart on a puffer or jacket, cheap fur "
    "trim on a hood, teddy-bear coats, aviator shearling, long ugg boots, long puffy snow boots, "
    "micro bags, mini backpacks, a crossbody belt bag, a soft shapeless lazy coat, mixing warm and "
    "cold beige in one outfit, or a head-to-toe single-pattern set. "
    # Доза и граница. Клиентка 30-50 одевается для работы и статуса, а не для подиума.
    "Use ONE deliberate trend accent per look — she should read as someone who knows what season "
    "it is, not as a costume. For daytime, work and business scenarios NEVER use sheer fabrics, "
    "corsets or bustiers, bare midriffs, low-rise waists or chainmail; those belong only to date "
    "and evening-event scenarios. Where a trend conflicts with her formula, her formula wins."
)

# Режиссура кадра. Без неё модель ставит человека в пустоту и получается карточка товара, а не
# образ, который хочется примерить на себя.
_EDITORIAL_DIRECTION = (
    " Compose it like a fashion editorial: confident relaxed posture, natural mid-stride or "
    "grounded stance, hands used naturally, gaze calm and direct or slightly off-camera. "
    "Real location with depth and atmosphere that suits the scenario — city street, gallery, "
    "hotel lobby, staircase, cafe terrace — never a plain studio backdrop or empty white void. "
    "Soft directional daylight, gentle shadows, shallow depth of field."
)


def render_look_on_client(client_photo: str, look_prompt: str, ref_image: str | None = None) -> str:
    """Identity-preserving рендер: фото клиентки + промпт образа → она в этом образе.

    Gemini 3 Pro image-to-image: держит лицо/волосы/фигуру, меняет только одежду.
    (GPT image отпал — OpenAI отказывается воссоздавать реальные лица.)
    look_prompt — это look-generator.looks[].image_generation_prompt. Возвращает data-URL.
    Фото-финиш (плёнка/текстура кожи) + канон капсулы (актуальный крой) вшиты в инструкцию.
    """
    instruction = (
        "Photo editing task: dress the SAME real woman from the reference photos in a new outfit. "
        "You are given TWO reference images of the SAME woman: the FIRST is a close-up of her "
        "head and face, the SECOND is a wider shot for her body and figure. "
        "The close-up is the GROUND TRUTH for her identity — study her face there carefully and "
        "reproduce it precisely so she is instantly recognizable as the exact same person. "
        "Do NOT replace her with a generic or idealised model.\n"
        "- Face: copy the SAME face from the close-up — face shape, eyes (shape and colour), nose, "
        "lips, eyebrows, skin tone and complexion, freckles and age. Do NOT beautify or alter it.\n"
        "- Age: she must look EXACTLY as old as in the close-up — never older. "
        "Do NOT add wrinkles, fine lines, eye bags, sagging, dull skin or grey hair that are not "
        "clearly visible in the reference. Ageing her is the worst possible error here.\n"
        # Клиентка приходит за образом, в котором она себе нравится. Черты лица не трогаем —
        # но показываем её в лучшей форме: отдохнувшей, с живой кожей и раскрытой осанкой.
        # Это работа света и подачи, а не пластики: «фото у хорошего фотографа», не другое лицо.
        "- Render her as the most vibrant, well-rested version of herself at her real age: "
        "healthy luminous skin, bright clear eyes, relaxed open posture, subtle natural make-up "
        "that suits the outfit. Flattering light and styling are welcome — changing her facial "
        "features, making her thinner, or swapping in a younger face is NOT.\n"
        "- Hair: keep the same colour, length and texture.\n"
        "- Body: keep the same height, build, weight and body proportions (figure type) from the "
        "wider shot. Do NOT slim, lengthen, or idealise her body — keep her real silhouette.\n"
        "Change ONLY her clothing and the background. "
        "Place her in a new location that fits the outfit's setting (do not reuse the reference background). "
        "Outfit and scene: " + look_prompt + _LOOK_CANON + _TREND_CANON
        + _EDITORIAL_DIRECTION + " Full-body head to toe, vertical 3:4 ratio." + _PHOTO_FINISH
    )
    model = config.MODELS["image"]["dressing"]
    # ДВА референса личности: (1) крупный кадр головы — чтобы лицо было в высоком разрешении и
    # модель не «додумывала» чужое (на ростовом фото лицо ~150px, этого мало); (2) фигура целиком
    # в 1536px. Если explicit ref_image передан — используем его как раньше (обратная совместимость).
    body = ref_image or provider.encode_image(client_photo, max_side=1536)
    face = provider.head_crop(client_photo, max_side=1024) if ref_image is None else None
    refs = [face, body] if face else [body]
    return provider.generate_image(instruction, model=model, ref_images=refs)[0]


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
