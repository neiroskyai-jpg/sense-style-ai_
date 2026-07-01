# -*- coding: utf-8 -*-
"""Генератор вау-базы капсул: 12 цветотипов × стиль → палитро-верная капсула.

Капсула собирается по канону метода:
- палитра строго из цветотипа (12-cvetotipov.md), табу-цвета исключены (stop_list);
- структура «верхов больше низов», 10-12 вещей, роли (база/слой/комплексный/статусный);
- бренды из нашего ядра (data/fashion-base/brands.csv) по сегменту + стилевому полю.

Запуск:  python scripts/gen_capsules.py
Выход:   data/fashion-base/capsules.json  +  data/fashion-base/capsule-showcase.md
"""
from __future__ import annotations
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "data", "fashion-base")

# ── Палитры 12 цветотипов (сжато из architecture/reference/colortypes/12-cvetotipov.md) ──
# base = нейтральная база (низы/слои), accent = цветовые акценты, taboo = стоп-лист.
PALETTES = {
 "Зима контрастная": dict(metals="серебро/платина",
   base=["чёрный","чистый белый","графит","navy","чернильно-синий"],
   accent=["чистый красный","фуксия","изумруд","сапфир/кобальт","рубин","ледяная мята"],
   taboo=["оранжевый","горчица","кэмел","тёплый беж","хаки","тёплое золото"]),
 "Зима натуральная": dict(metals="серебро/белое золото",
   base=["чернильно-синий","чёрный","графит","ледяной серый","кипенно-белый","винный"],
   accent=["изумруд","сапфир","фуксия","пурпур","рубиново-винный","баклажан"],
   taboo=["ivory","тёплый беж","персиковый","оранжевый","горчица","кэмел","хаки"]),
 "Зима светлая": dict(metals="серебро/платина",
   base=["navy","серо-синий","мягкий графит","холодный белый"],
   accent=["малиновый","светлая фуксия","ледяная роза","ледяной голубой","бирюза","изумруд"],
   taboo=["оранжевый","горчица","тёмный кэмел","тяжёлый чёрный","тёплые земляные"]),
 "Лето контрастное": dict(metals="серебро/белое золото",
   base=["приглушённый navy","slate-grey","какао","тауп","холодный белый"],
   accent=["малиновый","рубин","приглушённая фуксия","пыльная роза","сливовый","петроль","сапфир"],
   taboo=["чистый чёрный","оранжевый","тёплое золото","горчица","яркие тёплые"]),
 "Лето натуральное": dict(metals="серебро/белое золото",
   base=["серо-синий","slate","мягкое какао","тауп","мягкий молочный"],
   accent=["пыльная роза","лавандовый","приглушённый сливовый","шалфейно-бирюзовый","периивинкл"],
   taboo=["чёрный","графит","оранжевый","горчица","кэмел","алый","чистые яркие"]),
 "Лето светлое": dict(metals="серебро/платина",
   base=["серо-голубой","мягкий slate","припылённый тауп","холодный мягкий белый"],
   accent=["пыльная роза","ягодно-розовый","лавандовый","мята","пудрово-голубой","серо-сиреневый"],
   taboo=["чёрный","оранжевый","тёплое золото","тёмные земляные","резкий контраст"]),
 "Весна контрастная": dict(metals="золото/латунь",
   base=["золотисто-ореховый","кэмел","тёплый кремовый","тёплый белый"],
   accent=["тёплый красный/алый","коралл","бирюза","ярко-жёлтый","шартрез","кобальт"],
   taboo=["чёрный","холодный серый","припылённые грязные","ледяные пастели"]),
 "Весна натуральная": dict(metals="золото/бронза",
   base=["золотисто-коричневый","охра","кэмел","тёплый беж","тёплый кремовый"],
   accent=["коралл","тёплый малиновый","мандарин","sky blue","тёплый зелёный","тёплая горчица","бирюза"],
   taboo=["чёрный","ледяные холодные","холодный серо-голубой","холодная фуксия"]),
 "Весна светлая": dict(metals="золото тёплое",
   base=["тёплый беж","светлый кэмел","тёплый кремовый","светло-золотистый"],
   accent=["персик","светлый коралл","тёплый розовый","светло-жёлтый","тёплая мята","sky blue"],
   taboo=["чёрный","тёмные тяжёлые","холодные приглушённые","ледяные холодные"]),
 "Осень контрастная": dict(metals="золото/медь",
   base=["шоколад","тёмная олива","золотисто-коричневый","кэмел","тёплый айвори"],
   accent=["томатно-красный","терракота","тыквенный","золотисто-жёлтый","лесной зелёный","петроль-тиал","кирпич"],
   taboo=["чистый чёрный","чистый белый","ледяные пастели","холодная фуксия","серо-голубой"]),
 "Осень натуральная": dict(metals="золото/бронза",
   base=["шоколад","олива","золотисто-коричневый","тёплый беж","какао"],
   accent=["терракота","приглушённый коралл","охра","горчица","оливковый зелёный","петроль","кирпич"],
   taboo=["чёрный","ледяные холодные","чистая фуксия","холодный розовый","серебристо-голубой"]),
 "Осень светлая": dict(metals="золото/бронза",
   base=["тёплый беж","светлый кэмел","тёплый айвори","золотисто-коричневый мягкий"],
   accent=["коралл","лосось","персиково-охристый","светлая олива","тёплая бирюза","плам-мальва","тёплый жёлтый"],
   taboo=["чистый чёрный","холодные ледяные","яркая фуксия","сине-розовые"]),
}

# ── Стиль → характер капсулы (силуэт, подстиль, психология, слот-логика) ──
STYLES = {
 "classic": dict(name="Классика",
   silhouette="прямой / полуприлегающий, чистая линия",
   substyle="minimalism / quiet luxury",
   why=("Структура и чистая линия читаются как компетентность — образ говорит раньше тебя. "
        "База в нейтралях, один сдержанный акцент. Ничего лишнего, всё работает на статус."),
   accent_dose="один сдержанный акцент"),
 "drama": dict(name="Драма",
   silhouette="X / структурный силуэт, чёткое плечо",
   substyle="power woman / feminine drama",
   why=("Один сильный акцент держит внимание — ты входишь, и тебя видно. "
        "Контраст и графика в силуэте дают власть без агрессии."),
   accent_dose="сильный акцент + statement-аксессуар"),
 "romance": dict(name="Романтика",
   silhouette="X / мягкая линия, приталенность",
   substyle="lady-like / soft romantic",
   why=("Мягкая линия и тёплый акцент дают открытость без потери взрослости. "
        "Женственность здесь — инструмент, а не «девочковость»."),
   accent_dose="тёплый акцент, мягкая подача"),
 "natural": dict(name="Натуральный",
   silhouette="прямой / свободный, комфорт",
   substyle="smart casual / modern casual",
   why=("Лёгкость и комбинаторика — ты собрана и свободна одновременно. "
        "Каждая вещь стыкуется с 3-4 другими, образ живёт без усилий."),
   accent_dose="природный акцент, спокойно"),
}

# ── Пулы брендов по стилю: (бренд, сегмент). Ядро + узнаваемые для вау. ──
BRANDS = {
 "classic": [("12 STOREEZ","high"),("GATE31","middle"),("Massimo Dutti","high"),
             ("COS","high"),("Zarina","low"),("Charuel","high"),("Arny Praht","high"),("Mango","middle")],
 "drama":   [("Lichi","middle"),("IDOL","luxury"),("MasterPeace","high"),
             ("Love Republic","middle"),("Fashion Rebels","high"),("Mango","middle")],
 "romance": [("Love Republic","middle"),("Lusio","middle"),("Akhmadullina Dreams","high"),
             ("Charmstore","middle"),("Perles","high"),("True Red","high"),("Mango","middle")],
 "natural": [("Present & Simple","middle"),("2MOOD","middle"),("You Wanna","middle"),
             ("Lime","middle"),("Befree","low"),("Mango","middle"),("Uniqlo","low")],
}
# статусные аксессуары (кросс-сегмент)
BAGS = [("Ekonika","middle"),("Two-Ta","middle"),("Protégé","high"),("Mango","middle")]
JEWELRY = [("Avgvst","middle/high"),("Poison Drop","middle")]

# ── Скелет капсулы: (слот, роль, тип цвета) — верхов больше низов (4 верха / 2 низа) ──
SKELETON = [
 ("Брюки",            "низ · база",        "neutral"),
 ("Юбка миди / джинсы","низ · сопутствующий","neutral"),
 ("Рубашка",          "верх · база",       "neutral"),
 ("Тонкий джемпер",   "верх · база",       "base"),
 ("Блуза / топ",      "верх · акцент",     "accent"),
 ("Водолазка / трикотаж","верх · база",    "base"),
 ("Жакет",            "верхний слой · структура","neutral"),
 ("Пальто / тренч",   "верхний слой",      "base"),
 ("Платье",           "комплексный",       "accent"),
 ("Лоферы / ботинки", "обувь · база",      "neutral"),
 ("Каблук / мюли",    "обувь · акцент",    "accent"),
 ("Сумка",            "статусный",         "neutral"),
 ("Украшение / палантин","статусный · акцент","accent"),
]

# ── Раскладка цветотип → стиль (все 4 стиля представлены, под аудиторию 30-50) ──
ASSIGN = {
 "Зима контрастная":"drama","Зима натуральная":"classic","Зима светлая":"romance",
 "Лето контрастное":"classic","Лето натуральное":"romance","Лето светлое":"natural",
 "Весна контрастная":"drama","Весна натуральная":"natural","Весна светлая":"romance",
 "Осень контрастная":"drama","Осень натуральная":"natural","Осень светлая":"classic",
}


def pick(seq, i):
    return seq[i % len(seq)]


def slugify(s: str) -> str:
    import re
    tr = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя ",
                       "abvgdeejzijklmnoprstufhccss_y_eua-")
    return re.sub(r"-+", "-", s.lower().translate(tr)).strip("-")


def build_capsule(colortype: str, style_key: str) -> dict:
    pal = PALETTES[colortype]
    st = STYLES[style_key]
    pool = BRANDS[style_key]
    items = []
    bi = ai = ni = 0
    for slot, role, ctype in SKELETON:
        if ctype == "accent":
            color = pick(pal["accent"], ai); ai += 1
        elif ctype == "base":
            color = pick(pal["base"], bi); bi += 1
        else:
            color = pick(pal["base"] + pal["neutral"] if pal.get("neutral") else pal["base"], ni); ni += 1
        if slot == "Сумка":
            brand, seg = pick(BAGS, ni)
        elif slot.startswith("Украшение"):
            brand, seg = pick(JEWELRY, ai)
        else:
            brand, seg = pick(pool, len(items))
        items.append(dict(slot=slot, role=role, color=color, brand=brand, segment=seg))
    return dict(id=f"{slugify(colortype)}__{style_key}",
                colortype=colortype, style=st["name"], style_key=style_key,
                flagship=(ASSIGN.get(colortype) == style_key),
                silhouette=st["silhouette"], substyle=st["substyle"],
                metals=pal["metals"], why=st["why"], stop_list=pal["taboo"], items=items)


def render_md(capsules: list[dict]) -> str:
    L = ["# Вау-база капсул — 12 цветотипов × 4 стиля (48 капсул)",
         "",
         "> Сгенерировано `scripts/gen_capsules.py`. Палитра строго из цветотипа "
         "(`architecture/reference/colortypes/12-cvetotipov.md`), табу-цвета исключены. "
         "Бренды — из ядра `data/fashion-base/brands.csv` + узнаваемые. "
         "Структура по методу: 13 вещей, верхов больше низов, роли (база/слой/комплексный/статусный).",
         "",
         "**Как читать:** каждая капсула = цветотип × стилевой регистр. Цвет каждой вещи — из палитры "
         "цветотипа. ★ — флагманская капсула цветотипа (лучший стиль под типаж). "
         "Стоп-цвета внизу — их нельзя, даже если вещь красивая.",
         ""]
    order = list(PALETTES.keys())
    by_ct: dict[str, list[dict]] = {ct: [] for ct in order}
    for c in capsules:
        by_ct[c["colortype"]].append(c)
    for ct in order:
        L += [f"# {ct}", ""]
        for c in by_ct[ct]:
            star = " ★" if c["flagship"] else ""
            L += [f"## {c['colortype']} · {c['style']}{star}",
                  "",
                  f"*Силуэт:* {c['silhouette']} · *подстиль:* {c['substyle']} · *металлы:* {c['metals']}",
                  "",
                  "| Слот | Роль | Цвет | Бренд | Сегмент |",
                  "|---|---|---|---|---|"]
            for it in c["items"]:
                L.append(f"| {it['slot']} | {it['role']} | {it['color']} | {it['brand']} | {it['segment']} |")
            L += ["",
                  f"**Почему работает.** {c['why']}",
                  "",
                  f"**Стоп-цвета ({c['colortype']}):** " + ", ".join(c["stop_list"]) + ".",
                  "",
                  "---",
                  ""]
    return "\n".join(L)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    capsules = [build_capsule(ct, sk) for ct in PALETTES for sk in STYLES]
    with open(os.path.join(OUT_DIR, "capsules.json"), "w", encoding="utf-8") as f:
        json.dump(capsules, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT_DIR, "capsule-showcase.md"), "w", encoding="utf-8") as f:
        f.write(render_md(capsules))
    print(f"капсул сгенерировано: {len(capsules)}")
    print("→ data/fashion-base/capsules.json")
    print("→ data/fashion-base/capsule-showcase.md")


if __name__ == "__main__":
    main()
