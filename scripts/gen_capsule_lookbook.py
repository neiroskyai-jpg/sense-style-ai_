# -*- coding: utf-8 -*-
"""Лукбук-картинка к капсуле: палитро-верный editorial-кадр по одной капсуле.

Берёт капсулу из data/fashion-base/capsules.json, строит героический образ из её
РЕАЛЬНЫХ цветов (жакет/брюки/топ/платье/обувь), подставляет палитру цветотипа
и прогоняет через наш анти-пластик рецепт (плёнка, текстура кожи, без ретуши).

Запуск:
    python -m scripts.gen_capsule_lookbook <capsule_id>      # одна капсула
    python -m scripts.gen_capsule_lookbook --flagships       # 12 флагманов (по 1 на цветотип, ★)
    python -m scripts.gen_capsule_lookbook --all             # все 48
    #   + флаг --force — перегенерировать даже если картинка уже есть

Выход: web/photos/capsules/<capsule_id>.png
"""
from __future__ import annotations
import json, pathlib, sys

from core import provider

ROOT = pathlib.Path(__file__).resolve().parent.parent
CAPS = ROOT / "data" / "fashion-base" / "capsules.json"
OUT = ROOT / "web" / "photos" / "capsules"

# RU→EN для цветов палитр (что реально встречается в capsules.json)
COLOR_EN = {
 "чёрный": "true black", "чистый белый": "pure white", "холодный белый": "cool white",
 "кипенно-белый": "crisp cold white", "мягкий молочный": "soft milky white",
 "графит": "graphite grey", "мягкий графит": "soft graphite", "navy": "navy",
 "приглушённый navy": "muted navy", "чернильно-синий": "ink blue", "серо-синий": "slate blue",
 "серо-голубой": "grey-blue", "ледяной серый": "icy grey", "slate": "slate grey",
 "slate-grey": "slate grey", "мягкий slate": "soft slate", "какао": "cocoa brown",
 "мягкое какао": "soft cocoa", "тауп": "taupe", "припылённый тауп": "dusty taupe",
 "винный": "wine", "чистый красный": "pure red", "тёплый красный/алый": "warm scarlet red",
 "малиновый": "raspberry", "рубин": "ruby", "рубиново-винный": "ruby wine",
 "фуксия": "fuchsia", "светлая фуксия": "light fuchsia", "приглушённая фуксия": "muted fuchsia",
 "пурпур": "purple", "баклажан": "aubergine", "сливовый": "plum", "приглушённый сливовый": "muted plum",
 "изумруд": "emerald", "сапфир": "sapphire", "сапфир/кобальт": "sapphire cobalt",
 "кобальт": "cobalt", "петроль": "petrol teal", "петроль-тиал": "petrol teal",
 "бирюза": "turquoise", "тёплая бирюза": "warm turquoise", "шалфейно-бирюзовый": "sage teal",
 "ледяной голубой": "icy blue", "пудрово-голубой": "powder blue", "sky blue": "sky blue",
 "ледяная мята": "icy mint", "мята": "mint", "тёплая мята": "warm mint",
 "пыльная роза": "dusty rose", "ягодно-розовый": "berry pink", "тёплый розовый": "warm pink",
 "ледяная роза": "icy rose", "лавандовый": "lavender", "серо-сиреневый": "greyed lilac",
 "периивинкл": "periwinkle", "плам-мальва": "mauve plum",
 "золотисто-ореховый": "golden walnut brown", "золотисто-коричневый": "golden brown",
 "золотисто-коричневый мягкий": "soft golden brown", "шоколад": "chocolate brown",
 "тёмная олива": "dark olive", "олива": "olive", "охра": "ochre", "кэмел": "camel",
 "светлый кэмел": "light camel", "тёплый беж": "warm beige", "тёплый кремовый": "warm cream",
 "тёплый белый": "warm white", "тёплый айвори": "warm ivory", "тёплый кремовый мягкий": "soft warm cream",
 "коралл": "coral", "тёплый малиновый": "warm raspberry", "мандарин": "tangerine",
 "терракота": "terracotta", "тыквенный": "pumpkin", "кирпич": "brick red",
 "томатно-красный": "tomato red", "приглушённый коралл": "muted coral", "лосось": "salmon",
 "персиково-охристый": "peachy ochre", "персик": "peach", "светлый коралл": "light coral",
 "золотисто-жёлтый": "golden yellow", "ярко-жёлтый": "bright yellow", "тёплый жёлтый": "warm yellow",
 "светло-жёлтый": "light yellow", "тёплая горчица": "warm mustard", "горчица": "mustard",
 "шартрез": "chartreuse", "лесной зелёный": "forest green", "оливковый зелёный": "olive green",
 "тёплый зелёный": "warm green", "светлая олива": "light olive", "светло-золотистый": "light gold",
}


def en(color: str) -> str:
    return COLOR_EN.get(color.strip(), color.strip())


# Сцена/настроение по стилевому регистру
SCENE = {
 "classic": "poised and composed, minimal architectural setting, clean confident stance",
 "drama": "commanding and magnetic, strong editorial pose, dramatic directional light",
 "romance": "soft and warm, graceful relaxed posture, gentle diffused light",
 "natural": "easy and grounded, candid relaxed movement, airy daylight setting",
}

ANTIPLASTIC = (
 "Modern fashion editorial photograph: one elegant natural woman aged 35 to 48, "
 "candid editorial framing, generous negative space. "
 "Shot on Kodak Portra 400 film, Hasselblad medium-format, 85mm lens, shallow depth of field, "
 "authentic film grain, true-to-life colors, natural soft contrast. "
 "Real unretouched skin with visible texture, pores, fine lines and natural freckles, "
 "no beauty retouching, no skin smoothing, matte natural complexion, believable human proportions. "
 "Not plastic, not waxy, not glossy, not airbrushed, not CGI, not a 3D render, not over-saturated. "
 "No text, no words, no logos, no watermark. Vertical 4:5 portrait composition."
)


def hero_outfit(cap: dict) -> str:
    """Собрать фразу образа из реальных цветов ключевых слотов капсулы."""
    by_slot = {it["slot"]: it for it in cap["items"]}
    def c(slot):
        it = by_slot.get(slot)
        return en(it["color"]) if it else ""
    parts = []
    if "Жакет" in by_slot: parts.append(f"a tailored {c('Жакет')} blazer")
    if "Брюки" in by_slot: parts.append(f"{c('Брюки')} trousers")
    if "Блуза / топ" in by_slot: parts.append(f"a {c('Блуза / топ')} top as the colour accent")
    if "Пальто / тренч" in by_slot: parts.append(f"a {c('Пальто / тренч')} coat over the shoulders")
    if "Каблук / мюли" in by_slot: parts.append(f"{c('Каблук / мюли')} heels")
    return ", ".join(parts)


def palette_phrase(cap: dict) -> str:
    cols = []
    seen = set()
    for it in cap["items"]:
        e = en(it["color"])
        if e not in seen:
            seen.add(e); cols.append(e)
    return ", ".join(cols[:7])


def build_prompt(cap: dict) -> str:
    scene = SCENE.get(cap["style_key"], "")
    return (
        f"A {cap['style'].lower()} capsule wardrobe look for the {cap['colortype']} colour type. "
        f"The woman is {scene}, wearing {hero_outfit(cap)}. "
        f"Strict colour palette, use ONLY these colours in the clothing: {palette_phrase(cap)}. "
        f"Silhouette: {cap['silhouette']}. Metals: {cap['metals']}. "
        f"Absolutely avoid these colours entirely: {', '.join(cap['stop_list'])}. "
        + ANTIPLASTIC
    )


def generate_one(cap: dict, force: bool = False, verbose: bool = False) -> bool:
    """Сгенерировать кадр одной капсулы. True — успех, False — пропуск/ошибка."""
    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{cap['id']}.png"
    if dest.exists() and not force:
        print(f"= пропуск (уже есть): {cap['id']}")
        return False
    prompt = build_prompt(cap)
    if verbose:
        print("PROMPT:\n", prompt, "\n")
    try:
        urls = provider.generate_image(prompt)
    except Exception as e:
        print(f"✗ ошибка {cap['id']}: {str(e)[:160]}")
        return False
    provider.save_data_url(urls[0], dest)
    print(f"✓ сохранено: {dest.relative_to(ROOT)}")
    return True


def main():
    args = sys.argv[1:]
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if not args:
        print("Использование: python -m scripts.gen_capsule_lookbook <capsule_id> | --flagships | --all [--force]")
        raise SystemExit(1)

    caps = json.loads(CAPS.read_text(encoding="utf-8"))
    mode = args[0]
    if mode == "--all":
        targets = caps
    elif mode == "--flagships":
        targets = [c for c in caps if c.get("flagship")]
    else:
        cap = next((c for c in caps if c["id"] == mode), None)
        if cap is None:
            print(f"Капсула {mode} не найдена. Пример id: {caps[0]['id']}")
            raise SystemExit(1)
        targets = [cap]

    single = len(targets) == 1
    print(f"К генерации: {len(targets)} шт. (force={force})")
    ok = 0
    for cap in targets:
        if generate_one(cap, force=force, verbose=single):
            ok += 1
    print(f"\nГотово: {ok}/{len(targets)} сгенерировано.")


if __name__ == "__main__":
    main()
