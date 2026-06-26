# Sense Style AI — движок

AI-стилист с Vision-анализом: фото клиентки и ответы квиза → психологический
профиль стиля (Формула стиля), измеримый разрыв идентичности и капсула образов.
Методология — psychology-first (Self-Discrepancy Theory, Enclothed Cognition).

> Конкурсный MVP (Junior ML Contest, ИТМО). Источник правды по продукту —
> [`architecture/2026-04-27-mvp-spec.md`](architecture/2026-04-27-mvp-spec.md),
> по методу — [`architecture/sense-style-method.md`](architecture/sense-style-method.md).

## Архитектура

```
квиз Identity Scan ──┐
                     ├─→ vision-analyzer  → Claude/Gemini Vision  → колорит + фигура (JSON)
фото вещи / клиентки ┘
                       formula-diagnostic → диагностика           → Формула стиля + Identity Gap %
                       look-generator     → Seedream              → капсула образов
```

Интеллектуальное ядро — **промпт-библиотека** в [`architecture/prompts/`](architecture/prompts/)
(каждый промпт возвращает строгий JSON). Код — тонкая оркестрация поверх неё.

| Модуль | Назначение |
|---|---|
| `core/config.py` | маршрутизация моделей по тирам (dev/final), ключ из env |
| `core/prompts.py` | загрузка системных промптов из `architecture/prompts/*.md` |
| `core/provider.py` | клиент OpenRouter (OpenAI-совместимый), vision, сжатие фото |
| `core/pipeline.py` | сквозной пайплайн: vision → диагностика → (капсула) |
| `eval/` | метрики качества против экспертной разметки *(Фаза 3)* |

## Модели (через OpenRouter)

| Шаг | dev (отладка) | final (eval/демо) |
|---|---|---|
| Vision-анализ | `google/gemini-2.5-flash` | `anthropic/claude-sonnet-4.6` |
| Диагностика | `deepseek/deepseek-chat` | `anthropic/claude-sonnet-4.6` |
| Генерация образов | `bytedance/seedream-4` (+ Nano Banana для сравнения) | |

Тиринг: дёшево на отладке, Claude — на финальном качестве и eval. Провайдер
сменяем за конфиг (`core/provider.py`), без переписывания пайплайна.

## Запуск

```bash
pip install -r requirements.txt

# ключ — переменная окружения (в репозиторий не коммитим)
export OPENROUTER_API_KEY=sk-or-v1-...      # Windows: задать в «Переменные среды»

# vision-анализ фото
python -m scripts.run_vision портрет.jpg рост.jpg --height 165 --mode dev
```

## Тесты

```bash
pytest -q          # тесты загрузчика промптов (без обращения к API)
```

## Безопасность

Ключ OpenRouter хранится только в переменной окружения / `.env` (в `.gitignore`).
В коде и конфигах открытым текстом ключа нет.

## Статус

Фаза 1 (каркас + vision + диагностика) — в работе. План:
[`plans/2026-06-25-mvp-vertical-slice.md`](plans/2026-06-25-mvp-vertical-slice.md).
