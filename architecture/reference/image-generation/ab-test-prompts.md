# A/B-бейкофф генерации — готовые промпты на 4 эталонах (Фаза 1)

**Дата:** 2026-06-16
**К плану:** `plans/2026-06-16-image-gen-ab-bakeoff.md` (Фаза 1 — сбор входов).
**Структура:** 6-блочная из `image-generation.md` §2. Один и тот же промпт прогоняется через Nano Banana / Seedream 4.5 / gpt-image; Seedream дополнительно получает образ-референс (мудборд подтипа). Исходное фото клиентки идёт как image-to-image reference (сохранение лица/фигуры).

**Как читать:** RU-шапка = провалидированные входы и что тестирует субъект. EN-блок = собственно промпт для image-API. Negative-блок строится из табу палитры + стоп-силуэтов формулы.

---

## Субъект 1 — Мария Ухина (winter_natural · hourglass · холодная)

**Входы (кейс `cases/2026-05-03-mariya-uhina.md`):** winter_natural, cool, контраст medium, песочные часы 98-67-99, рост 1.68. Формула: Лэди-лайк × Шанель-классика × эффектная драма-нота. Сценарий: трансфер в аэропорту / свидание.
**Тестирует:** холодную приглушённую палитру самоцветов + чёткую талию песочных часов + НЕ-красный акцент (красный у неё в табу — проверка, не подсунет ли модель красный).

```
Photorealistic editorial full-body portrait of [REFERENCE PHOTO — preserve her face and hourglass figure exactly], woman in her late 30s.
Wearing a lady-like Chanel-inspired look with a clearly defined waist: a belted midi dress and a structured tweed jacket with nipped waist, or a tucked silk blouse with a pencil midi skirt. Fabrics: wool, tweed, silk, premium knit. Cool natural-winter jewel palette only — ink navy, cobalt, aubergine plum, emerald and dark green, graphite, cool wine, ice white, taupe. One vintage feminine accent: a printed silk scarf, statement stud earrings, and a structured handbag in a cold jewel tone (wine or emerald or sapphire). Low kitten-heel leather shoes (under 4 cm).
Scene: walking confidently through a sleek modern airport terminal, daylight.
Shot on Hasselblad X1D, Agfa Vista Plus 200 film tone, clean even light, Vogue editorial style.
Hyperrealistic professional fashion photography, real editorial photoshoot, sharp focus.
Negative: red, neon, mini skirt, sporty jacket, oversized boxy silhouette, warm beige, cream, ivory, peach, gold tones, heels over 4 cm, distorted face, distorted hands.
```

---

## Субъект 2 — Ксения Колупаева (autumn_natural · rectangle→Х · ТЁПЛАЯ)

**Входы (кейс `cases/2026-05-03-ksenia-kolupaeva.md`, ПАЛИТРА скорректирована под провалидированный тёплый цветотип — см. находку в плане):** autumn_natural, warm, прямоугольник со склонностью к Х, рост 1.76. Формула: Quiet Luxury × Парижский шик × лэди-лайк нота. Сценарий: деловая встреча tech-leader / городская повседневность.
**Тестирует:** тёплую осеннюю палитру (главный тест цвета — не уйдёт ли модель в холод/беж по умолчанию) + чистую линию прямоугольника без футляра + Quiet Luxury без логотипов.

```
Photorealistic editorial full-body portrait of [REFERENCE PHOTO — preserve her face and tall rectangular figure exactly], woman around 39, height 176 cm, long legs.
Wearing a Quiet-Luxury Parisian-chic look with clean semi-fitted lines: straight ankle trousers with a tucked oversized shirt, or a belted wrap dress, or a midi skirt with fine knit. Fabrics: cashmere, dense cotton, silk, wool, premium knit, no logos. Warm natural-autumn palette only — chocolate brown, olive, golden brown, camel, warm ivory, terracotta, mustard, forest and olive green, teal petrol, brick, muted coral. One lady-like feminine detail: a structured handbag, a silk scarf, or stud earrings. Loafers or low leather boots or kitten heels.
Scene: confident business meeting of a tech leader, or a refined city street, warm daylight.
Shot on Hasselblad X1D, Kodak Portra film tone, soft warm golden daylight, editorial minimal style.
Hyperrealistic professional fashion photography, real editorial photoshoot, sharp focus.
Negative: icy cold pastels, cold fuchsia, cold grey-blue near the face, navy in the portrait zone, neon, heels over 5 cm, sporty total look, distorted face, distorted hands.
```

---

## Субъект 3 — Анна Овешкова (summer_natural · hourglass · холодная мягкая)

**Входы (кейс в плане `2026-06-12-vision-analyzer.md`, формула СИНТЕЗИРОВАНА для теста из цветотипа+фигуры):** summer_natural, cool, контраст low, песочные часы. Подстиль для теста: мягкая натуральная элегантность / smart casual. Сценарий: дневное кафе / прогулка по городу в выходной.
**Тестирует:** приглушённую холодную палитру с НИЗКИМ контрастом (модель любит добавлять контраст — проверка, удержит ли мягкость) + мягко выраженную талию.

```
Photorealistic full-body portrait of [REFERENCE PHOTO — preserve her face and soft hourglass figure exactly], woman in her 30s.
Wearing a soft, gently waist-defined smart-casual look in a low-contrast outfit: a soft drape midi dress with a thin belt, or a fine knit tucked into a soft midi skirt. Fabrics: soft drape, fine knit, silk. Cool natural-summer muted palette only, all low-contrast and tonal — soft grey-blue, slate, dusty rose, mauve lavender, muted plum, sage-teal, periwinkle, dusty blue, soft milky white. No sharp contrasts, gentle tonal blending. One soft feminine detail.
Scene: relaxed daytime in a bright café or a weekend city walk, soft overcast daylight.
Shot on Canon EOS R6, Kodak Portra film tone, soft diffused daylight, low contrast, gentle.
Hyperrealistic professional fashion photography, real editorial photoshoot, sharp focus.
Negative: black, high contrast, hard shadows, warm gold, orange, golden brown, camel, neon, bright saturated warm colors, distorted face, distorted hands.
```

---

## Субъект 4 — «весна» (spring · warm · light · low contrast)

**Входы (Vision-чтение, формула и фигура СИНТЕЗИРОВАНЫ для теста — фигуры нет, только рост):** spring (светлая/натуральная), warm, light, low contrast. Подстиль для теста: свежая натурально-романтичная лёгкость. Сценарий: солнечный день, сад / залитая светом улица.
**Тестирует:** тёплую СВЕТЛУЮ чистую палитру (граница с холодным летом — самый частый промах авто-инструментов; проверка, не уведёт ли модель в холод или в тяжёлый тёмный).

```
Photorealistic full-body portrait of [REFERENCE PHOTO — preserve her face and figure exactly], young woman, light and fresh appearance.
Wearing a fresh natural-romantic light look, semi-fitted: a soft warm-toned dress or a light blouse with light trousers or a flowy skirt. Warm light-spring clear palette only — warm beige, light camel, warm cream, peach, light coral, warm soft pink, light yellow, warm mint, sky blue, light warm green. Clear light fresh tones, low contrast. One gentle feminine detail.
Scene: bright sunny daytime in a green garden or a sunlit street.
Shot on Fujifilm XT5, Fujifilm Pro 400H film tone, bright warm natural daylight.
Hyperrealistic professional fashion photography, real editorial photoshoot, sharp focus.
Negative: black, cold icy pastels, heavy dark colors, cold grey-blue, muted greyed tones, neon, distorted face, distorted hands.
```

---

## Что делать с этими промптами (Фазы 2–4)

1. **Прогон:** каждый из 4 → через OpenRouter на Nano Banana / Seedream 4.5 (+ образ-референс) / gpt-image. По 2–3 прогона на стабильность. Исходное фото — как image-to-image reference.
2. **Слепая оценка Ксенией** по рубрике плана: идентичность (gate, ×2) → цвет (сверка с палитрой `12-cvetotipov.md`) → реализм → фигура (`figure-correction`) → формула.
3. **Решение** → победитель в `2026-05-19-ai-photo-generation.md` (Фаза 1) + синтаксис API в `image-generation.md`.

> Нужен ключ OpenRouter (фаундер). Промпты готовы — прогон не требует доработки текста.
