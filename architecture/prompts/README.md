# architecture/prompts/

Системные промпты для AI-слоя Sense Style. Каждый промпт — рабочий артефакт, готовый к использованию через Claude API.

## Принципы

1. **Промпт = производный от методологии.** Источник правды — `architecture/sense-style-method.md`. Если методология меняется — промпты должны быть синхронизированы.
2. **Always JSON.** Все промпты возвращают валидный JSON по схеме. Никакого markdown-парсинга в production.
3. **Версионирование.** Каждое изменение промпта = новая версия (v1.1, v1.2). Старые версии — в `archive/`.
4. **Валидация на тест-кейсах.** Перед production — обязательный прогон на 10-15 кейсах с участием Ксении как валидатора.

## Файлы

| Файл | Что внутри | Статус |
|---|---|---|
| `formula-diagnostic.md` | Главный промпт: применяет 7-шаговый алгоритм к ответам квиза, возвращает Формулу стиля + расчёт разрыва идентичности + preview-образ | ✅ v1.0 готов, требует валидации на кейсах |
| `style-library.md` | Справочник атрибутов стилей (цвета, принты, ткани, бренды, правила микса) по 25 подстилям. Knowledge base, подключается к `look-generator` и `wardrobe-analyzer`. Источник данных — модуль «Алгоритмы имиджа» | ✅ v1.0, требует валидации Ксенией |
| `look-generator.md` | По Формуле стиля собирает капсулу (связанный набор вещей с максимумом комбинаций) и готовые образы под сценарии. Использует `style-library.md`. Алгоритм капсулы — из модуля «Алгоритм создания капсулы» | ✅ v1.0, требует валидации Ксенией |
| `style-book.md` | Финальная сборка: из выходов `formula-diagnostic` + `look-generator` и 4 баз собирает Style Book (9 разделов) в tone of voice Ксении. Возвращает JSON по разделам. Эталон — `cases/2026-06-09-mishel-style-book.md` | ✅ v1.0, требует валидации Ксенией |
| `vision-analyzer.md` | Vision-мост: фото клиентки → JSON с цветотипом (1 из 12), контрастом, природной палитрой (hex-пигменты) и типом фигуры (1 из 5) с пропорциями. Выход стыкуется со входом `formula-diagnostic`. Методы чтения — `reference/colortypes/photo-reading.md` + `reference/figure-correction/body-reading.md` | ✅ v1.0, не тестирован (план: `plans/2026-06-12-vision-analyzer.md`) |
| `shopping-list.md` | По капсуле (выход `look-generator`) + бюджет/поля/палитра/фигура подбирает бренды (из `reference/shopping/brand-matrix.md`) и готовый поисковый запрос под каждую недостающую вещь. Приоритет по «5 группам товаров», антипотребительский принцип (teaser 2-3 / full). Фаза 1 шопинга | ✅ v1.0, не тестирован (зависит от brand-matrix v0.1) |
| `wardrobe-analyzer.md` | (TODO) Промпт для анализа имеющегося гардероба (тариф «Книга стиля») | ⏳ |
| `mini-scan.md` | (TODO) Промпт для повторной мини-диагностики (трекер разрыва идентичности, P2) | ⏳ |

## Workflow промпт-инжиниринга

1. Написать промпт в новом `.md` файле
2. Тестировать через `skill-creator` (`.claude/skills/skill-creator/`) на 5-10 кейсах
3. Прогнать через Ксению-валидатора (соответствует ли её интуитивному определению)
4. Зафиксировать как v1.0
5. Дальше — итерации с инкрементами версий

## Production-использование

```python
from anthropic import Anthropic
import json

client = Anthropic()

# Загружаем промпт из markdown-файла, извлекая SYSTEM PROMPT блок
with open("architecture/prompts/formula-diagnostic.md") as f:
    content = f.read()
    system_prompt = extract_system_prompt(content)  # парсим markdown секцию

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    system=system_prompt,
    messages=[
        {"role": "user", "content": json.dumps(quiz_answers, ensure_ascii=False)}
    ]
)

# Ответ всегда валидный JSON
result = json.loads(response.content[0].text)
```

## Принцип AI + стилист

В каждом промпте обязательно поле `requires_stylist_validation` — список зон,
требующих ручной валидации Ксенией перед отправкой клиентке. Это не «слабость
AI», а методологическая принципиальность: AI ускоряет, стилист обеспечивает
точность.
