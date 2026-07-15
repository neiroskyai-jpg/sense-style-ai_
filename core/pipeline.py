"""Сквозной пайплайн: vision-анализ → диагностика Формулы стиля → (капсула).

Шаги 1-2 (vision + диагностика) реализованы. Шаг 3 (look-generator + генерация
образов через Seedream) подключается в Фазе 2 — см. plans/2026-06-25-mvp-vertical-slice.md.
"""
from __future__ import annotations
import json
from urllib.parse import quote_plus

from . import config, provider
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
- ЗАПРЕЩЕНЫ чистые спектральные и неоновые цвета: #0000FF, #FF00FF, #00FFFF, #FFFF00, #00FF00 \
и любые кислотные. Таких тканей не существует. Даже яркий цвет у Зимы — это плотный текстильный \
оттенок (королевский синий #2B4C9B, фуксия #A83A6B, изумруд #0B6E4F), а не спектр из палитры Paint.
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
    "во всех образах. Варьируй силуэт, длину, верхний слой и обувь между образами.\n"
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
    # грунтуем подбор явными правилами посадки под фигуру (размеры/силуэты считываются при подборе)
    fit_prompt = fit_rules_prompt(diagnosis.get("figure_type"))
    if fit_prompt:
        system += "\n\n# ПОСАДКА ПОД ФИГУРУ (обязательно к соблюдению)\n\n" + fit_prompt
    system += "\n\n# КАЧЕСТВО КАПСУЛЫ (обязательно к соблюдению)\n\n" + _CAPSULE_QUALITY_RULES
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
    "Real unretouched skin with visible texture, pores and fine lines, no beauty retouching, "
    "no skin smoothing, matte natural complexion, candid documentary feel. "
    "Not plastic, not waxy, not glossy, not airbrushed, not CGI, not a 3D render, not over-saturated. "
    "No text, no logos, no watermark."
)

# Правила капсулы в рендере (по канону «Алгоритмы имиджа», см. base-vs-trend.md) — страховка на случай,
# если image-модель «сдрейфует» в устаревший силуэт. Держим актуальный крой в самой картинке.
_LOOK_CANON = (
    " The outfit must read current and wearable: clean modern cut, full-length sleeves (never 3/4), "
    "structured outerwear with room for layers, well-proportioned silhouette. "
    "No dated styling: no skinny jeans with chunky boots, no cheap fur trim, no bulky mini-backpack."
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
        "- Hair: keep the same colour, length and texture.\n"
        "- Body: keep the same height, build, weight and body proportions (figure type) from the "
        "wider shot. Do NOT slim, lengthen, or idealise her body — keep her real silhouette.\n"
        "Change ONLY her clothing and the background. "
        "Place her in a new location that fits the outfit's setting (do not reuse the reference background). "
        "Outfit and scene: " + look_prompt + _LOOK_CANON
        + " Full-body head to toe, vertical 3:4 ratio." + _PHOTO_FINISH
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
