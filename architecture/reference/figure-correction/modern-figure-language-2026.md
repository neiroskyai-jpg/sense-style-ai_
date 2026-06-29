# Современный язык описания фигуры и стиля (2026, международная практика)

**Тип:** knowledge base для AI-слоя. Задаёт ЛЕКСИКУ описания фигуры и стиля — питает
`vision-analyzer` (как описывать тело), `formula-diagnostic` (dna_explanation, visual_formula),
look-generator и тексты направлений. Дополняет `figure-correction.md` (там — приёмы коррекции,
здесь — как это называть клиентке).

**Принцип (2026):** за рубежом (Европа/США) ушли от «фруктово-геометрической» классификации
(яблоко, груша, прямоугольник) к **пропорциям, линиям и балансу** (Body Liberation). Старые
термины ещё понятны, но в премиум-сегменте считаются устаревшими и ограничивающими. Ценится
описание через **визуальный баланс, доминирующие линии, особенности посадки одежды**.
Продвинутые AI-сервисы реагируют не на «pear shape», а на маркеры **пропорций** (vertical
lines, structural, balanced) и **текстур** (fluid, crisp, substantial).

## 1. Типы фигуры → язык пропорций и линий

Описывать фигуру через три параметра: **баланс верха/низа**, **акцент на талии**,
**длина торса/ног**. Внутренние коды движка (rectangle/pear/apple/…) НЕ меняем — это только
язык описания на выходе.

| Код движка | Старое (не использовать) | Современно (RU) | EN-маркеры |
|---|---|---|---|
| `rectangle` | Прямоугольник | Прямой силуэт, сбалансированные пропорции; атлетичные линии | balanced, athletic build, straight silhouette, column-like |
| `pear` | Груша | Объём в бёдрах, выраженная талия | lower-body dominant with a defined waist, hips-forward |
| `hourglass` | Песочные часы | Сбалансированные пропорции (плечи/бёдра), естественно выраженная талия | balanced proportions, naturally defined waist, curvaceous |
| `inverted_triangle` | Перевёрнутый треугольник | Выраженная линия плеч, более узкие бёдра | strong/structured shoulders, upper-body dominant |
| `apple` | Яблоко | Мягкие линии, объём в центре, стройные ноги | softer lines, weight centered in midsection, slender legs |

## 2. Fit Challenges (особенности посадки) — добавляют глубину

За рубежом всегда спрашивают про «боли» посадки. Использовать в диагностике/рекомендациях:
- Длинный торс / короткие ноги — или наоборот (long torso / shorter legs).
- Брюки хороши в бёдрах, но велики в талии (gaping waist in trousers).
- Широкие плечи, нужна свобода движения (broad shoulders needing ease of movement).
- Предпочтение средней/высокой посадки для чистых вертикальных линий (mid-to-high rise → clean vertical lines).

## 3. Современные дескрипторы стиля (2026)

Для премиальной, авторитетной подачи (вместо плоского «минимализм/классика»):
- **Elevated Minimalism** — благородный/утончённый минимализм (глубже, чем «minimalism»).
- **Quiet Luxury with architectural lines** — тихая роскошь, чёткие выверенные формы, премиальные ткани.
- **Soft Armor Tailoring** — главный термин 2026: жакеты/пальто, что держат форму, но лёгкие и пластичные.
- **Power Tailoring / «Glamoratti»** — сдержанно и дорого, но с сильным акцентом (чёткое плечо в духе Saint Laurent).
- **Relaxed Elegance / Intentional Dressing** — непринуждённая элегантность, удобство, что выглядит дорого (струящиеся брюки, шёлк, качественный трикотаж).

**Маркеры текстур:** fluid (струящийся), crisp (чёткий), substantial (плотный, держащий форму) — избегать clingy (облегающий, «прилипающий»).

## 4. Шаблон блока (как формулировать)

> **Proportions:** Balanced, straight silhouette with athletic undertones. Stronger shoulder line, mid-height, visually balanced hips.
> **Fit Challenges:** Clothing that allows ease of movement without losing structure; mid-to-high rises for clean vertical lines.
> **Fabric:** Substantial, mid-weight fabrics (wool, dense cotton, silk) that hold architectural shapes; avoid clingy materials.
> **Aesthetic:** Elevated minimalism and quiet luxury with a "soft armor" twist — structured blazers, relaxed wide-leg trousers, tonal dressing that projects effortless authority.

## 5. Как подключается

- `vision-analyzer`: описывать фигуру языком пропорций/линий, фиксировать fit challenges, не «фруктами».
- `formula-diagnostic`: `dna_explanation` и `visual_formula` — современные дескрипторы стиля и тканей.
- Отображение клиентке: короткие RU-лейблы из таблицы раздела 1 (`_FIGURE_LABEL` в app/main.py).
- При расхождении с подходом Ксении — выигрывает Ксения; документ правится.
