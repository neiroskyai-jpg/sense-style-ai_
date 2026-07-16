# -*- coding: utf-8 -*-
"""Сборка презентации подачи ИТМО в слайды (.pptx) из submission/02-презентация.md.

Зачем: конкурс просит СЛАЙДЫ, а не Word. Контент по 8 слайдам (с таймингом и репликами) уже написан —
этот скрипт превращает его в реальный .pptx, который открывается в PowerPoint и правится руками.

Шрифты: Georgia (сериф с кириллицей, есть на любом Windows) вместо брендового Cormorant Garamond —
Cormorant в системе не установлен, PowerPoint молча подменил бы его на Calibri и сломал вёрстку на
чужой машине/проекторе. Палитра — Petrogradka Editorial из architecture/visual-direction.md.

Реплики докладчика кладём в заметки к слайдам (view: Заметки) — их видно в режиме докладчика.

ЗАПУСК:
    python scripts/build_submission_pptx.py
    python scripts/build_submission_pptx.py --out submission/pptx/prezentaciya.pptx
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "submission" / "pptx" / "02-презентация.pptx"

# Petrogradka Editorial
CREAM = RGBColor(0xF5, 0xEF, 0xE3)
INK = RGBColor(0x1F, 0x1D, 0x1B)
WINE = RGBColor(0x5D, 0x22, 0x30)
MUTED = RGBColor(0x6B, 0x64, 0x5C)
LINE = RGBColor(0xE3, 0xDC, 0xCF)

SERIF = "Georgia"       # заголовки (брендовый Cormorant не установлен — см. докстринг)
SANS = "Segoe UI"       # текст


def _bg(slide, prs):
    """Кремовый фон — на всю площадь слайда."""
    from pptx.enum.shapes import MSO_SHAPE
    r = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    r.fill.solid()
    r.fill.fore_color.rgb = CREAM
    r.line.fill.background()
    r.shadow.inherit = False
    slide.shapes._spTree.remove(r._element)
    slide.shapes._spTree.insert(2, r._element)   # фон — под остальным


def _text(slide, left, top, width, height, text, size, font=SANS, color=INK,
          bold=False, align=PP_ALIGN.LEFT, italic=False, spacing=1.15):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = spacing
        run = p.add_run()
        run.text = ln
        run.font.size = Pt(size)
        run.font.name = font
        run.font.bold = bold
        run.font.italic = italic
        run.font.color.rgb = color
    return box


def _rule(slide, left, top, width):
    """Тонкая винная линия — тот же приём, что в вёрстке Карты."""
    from pptx.enum.shapes import MSO_SHAPE
    r = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top), Inches(width), Pt(2))
    r.fill.solid()
    r.fill.fore_color.rgb = WINE
    r.line.fill.background()
    r.shadow.inherit = False


def _eyebrow(slide, text):
    _text(slide, 0.9, 0.5, 8, 0.3, text.upper(), 11, SANS, WINE, bold=True)
    _rule(slide, 0.9, 0.85, 11.5)


def _notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


# Кадры «до/после» с лендинга: те же, что уже опубликованы на сайте (фаундер, согласие есть).
# Эмоциональный якорь презентации — по заметкам к контенту повторяются на титуле и в результатах.
PHOTO_BEFORE = ROOT / "web" / "photos" / "hero" / "01-do.jpg"
PHOTO_AFTER = ROOT / "web" / "photos" / "hero" / "02-posle.jpg"


def _pair_photos(slide, left, top, height, caption_size=11, slide_w=13.333):
    """Два кадра «до/после» рядом + подписи. Кадры вертикальные 3:4 — ширину считаем от высоты.

    Высоту ужимаем, если пара не влезает в остаток слайда: два вертикальных кадра съедают ширины
    больше, чем кажется (h=4.9" → 2×3.68" + зазор = 7.5"), и молча уезжают за край.
    """
    gap = 0.18
    avail = slide_w - left - 0.35          # правое поле
    width = height * 0.75
    if 2 * width + gap > avail:
        width = (avail - gap) / 2
        height = width / 0.75
    for i, (path, cap) in enumerate([(PHOTO_BEFORE, "до"), (PHOTO_AFTER, "после")]):
        if not path.exists():
            continue
        x = left + i * (width + gap)
        slide.shapes.add_picture(str(path), Inches(x), Inches(top),
                                 width=Inches(width), height=Inches(height))
        _text(slide, x, top + height + 0.04, width, 0.3, cap.upper(), caption_size, SANS,
              WINE if cap == "после" else MUTED, bold=True, align=PP_ALIGN.CENTER)


def _bullets(slide, items, top=2.3, size=17, gap=0.62, width=11.0):
    for i, it in enumerate(items):
        _text(slide, 1.15, top + i * gap, width, 0.55, "— " + it, size, SANS, INK)


def build(prs: Presentation) -> None:
    blank = prs.slide_layouts[6]

    # ── 1. Титул ───────────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _text(s, 0.9, 2.0, 7.4, 1.2, "Sense Style AI", 54, SERIF, INK)
    _rule(s, 0.9, 3.25, 4.2)
    _text(s, 0.9, 3.5, 7.4, 0.9, "Персональный стилист\nна основе психологии моды", 22, SERIF, WINE, italic=True)
    _text(s, 0.9, 4.75, 7.4, 1.2,
          "Измеряем разрыв между тем, какой женщину видят сейчас,\nи тем, какой она хочет быть — и закрываем его гардеробом.",
          15, SANS, MUTED)
    _text(s, 0.9, 6.4, 7.4, 0.5, "Ксения Колупаева  ·  ИИ-стартап  ·  Junior ML Contest ИТМО, 2026", 13, SANS, MUTED)
    _pair_photos(s, left=7.5, top=1.6, height=4.4)
    _notes(s, "0:00–0:20\n\n«Sense Style AI измеряет разрыв между тем, какой женщину видят сейчас, "
              "и тем, какой она хочет быть, — и собирает гардероб, который этот разрыв закрывает.»\n\n"
              "ВИЗУАЛ: кадры «до/после» — те же, что на лендинге. Эмоциональный якорь, повторяется "
              "на слайде 7. Можно заменить на кадр клиентки, если будет согласие.")

    # ── 2. Проблема ────────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "01 · Проблема")
    _text(s, 0.9, 1.15, 11.5, 0.9, "«Полный шкаф, а надеть нечего»", 36, SERIF, INK)
    _bullets(s, [
        "Женщина 30–50 в точке перехода: новая должность, материнство, переезд",
        "Внутренне изменилась — а образ остался прежним",
        "Она не выглядит как та, кем стала",
        "AI-стилисты советуют вещи по трендам — это не решает проблему",
    ])
    _text(s, 1.15, 5.2, 11, 0.8, "Проблема не в вещах. Проблема — в разрыве идентичности.",
          22, SERIF, WINE, italic=True)
    _notes(s, "0:20–1:00\n\n«Женщина в точке перехода — новая должность, материнство, переезд — внутренне "
              "изменилась, а образ остался прежним. Она не выглядит как та, кем стала. Существующие "
              "AI-стилисты советуют вещи по трендам. Это не решает проблему: проблема не в вещах, "
              "а в разрыве идентичности.»\n\nВИЗУАЛ: портрет ЦА.")

    # ── 3. Решение и наука ─────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "02 · Решение и научная опора")
    _text(s, 0.9, 1.15, 11.5, 0.9, "Identity Gap — разрыв в процентах", 36, SERIF, INK)
    _text(s, 0.9, 2.15, 5.6, 1.6, "62%", 96, SERIF, WINE)
    _text(s, 0.9, 3.9, 5.6, 0.9, "средний стартовый разрыв\n(4 клиентки, разброс 50–78%)", 14, SANS, MUTED)
    _text(s, 6.9, 2.3, 5.6, 0.5, "Научная опора", 18, SERIF, INK, bold=True)
    _bullets_right = [
        ("Self-Discrepancy Theory", "Higgins, 1987 — расхождение actual / ideal self"),
        ("Enclothed Cognition", "Adam & Galinsky, 2012 — одежда меняет восприятие"),
    ]
    top = 2.95
    for title, sub in _bullets_right:
        _text(s, 6.9, top, 5.6, 0.4, title, 15, SANS, WINE, bold=True)
        _text(s, 6.9, top + 0.38, 5.6, 0.4, sub, 13, SANS, MUTED)
        top += 1.0
    _text(s, 6.9, 5.0, 5.6, 1.2,
          "Поверх — авторская методология:\n4 стиля · 25 подстилей · 7-шаговый алгоритм.\nМетодология и есть наша разметка данных.",
          14, SANS, INK)
    _notes(s, "1:00–1:45\n\n«Мы операционализировали разрыв идентичности в процент — Identity Gap. Опора "
              "научная: Self-Discrepancy Theory даёт саму идею расхождения actual/ideal self, Enclothed "
              "Cognition — влияние одежды на восприятие. Поверх — авторская методология: 4 стиля, "
              "25 подстилей, 7-шаговый алгоритм Формулы стиля. Методология и есть наша разметка данных.»")

    # ── 4. Пайплайн ────────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "03 · Как это работает")
    _text(s, 0.9, 1.15, 11.5, 0.9, "Пайплайн: от анкеты до образа на клиентке", 34, SERIF, INK)
    steps = [
        ("Квиз + фото", "мультимодальный вход"),
        ("Vision", "колорит, геометрия фигуры"),
        ("RAG", "правила метода, объяснимость"),
        ("Диагностика", "Формула стиля + Identity Gap"),
        ("Генерация", "образы на её лице и фигуре"),
    ]
    x = 0.9
    for i, (title, sub) in enumerate(steps):
        _text(s, x, 2.6, 2.1, 0.4, title, 15, SANS, WINE, bold=True)
        _text(s, x, 3.05, 2.1, 0.8, sub, 12, SANS, MUTED)
        if i < len(steps) - 1:
            _text(s, x + 1.95, 2.55, 0.4, 0.4, "→", 20, SANS, WINE)
        x += 2.42
    _text(s, 0.9, 4.6, 11.5, 1.4,
          "Ключевое: Identity Gap считается ОДИН раз на сервере — и одинаково виден на всех экранах.\n"
          "Не бывает «на квизе одно число, на Карте другое».",
          17, SANS, INK)
    _text(s, 0.9, 5.9, 11.5, 0.6, "Полная Карта стиля собирается за ~66 секунд (живой прогон на проде).",
          15, SERIF, WINE, italic=True)
    _notes(s, "1:45–2:45\n\n«Вход мультимодальный: анкета и фото. Vision читает колорит и геометрию фигуры. "
              "RAG подмешивает правила метода и объясняет диагноз. Диагностика выдаёт Формулу стиля и "
              "Identity Gap. Генератор примеряет новые образы на её собственное лицо и фигуру. Ключевое: "
              "Gap считается один раз на сервере и одинаково виден на всех экранах — не бывает "
              "«на квизе одно число, на карте другое».»")

    # ── 5. Применение ИИ ───────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "04 · Применение ИИ")
    _text(s, 0.9, 1.15, 11.5, 0.9, "Три ИИ-способности в одном продукте", 34, SERIF, INK)
    cards = [
        ("Мультимодальность", "Vision читает фото:\nколорит, контраст,\nгеометрия фигуры"),
        ("RAG с объяснимостью", "Retrieval правил метода\nиз авторской базы —\nдиагноз объясним"),
        ("Identity-preserving\nгенерация", "Образы на её собственном\nлице и фигуре, а не\nна абстрактной модели"),
    ]
    x = 0.9
    for title, body in cards:
        _text(s, x, 2.5, 3.6, 0.8, title, 17, SANS, WINE, bold=True)
        _text(s, x, 3.5, 3.6, 1.5, body, 13, SANS, INK)
        x += 3.9
    _text(s, 0.9, 5.5, 11.5, 1.0,
          "Интеллект вынесен в версионируемую промпт-библиотеку.\n"
          "Провайдер моделей сменяем за один модуль — мы не привязаны к одной сети.",
          15, SANS, MUTED)
    _notes(s, "2:45–3:15\n\n«Три ИИ-способности вместе: мультимодальный анализ, retrieval доменных знаний "
              "с объяснимостью и генерация с сохранением идентичности. Интеллект вынесен в "
              "версионируемую промпт-библиотеку; провайдер моделей сменяем за один модуль — мы не "
              "привязаны к одной сети.»\n\nМодели: Vision + генерация через OpenRouter.")

    # ── 6. Data Science ────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "05 · Data Science: оценка")
    _text(s, 0.9, 1.15, 11.5, 0.9, "Меряем диагностику против экспертной разметки", 32, SERIF, INK)

    rows, cols = 3, 4
    tbl = s.shapes.add_table(rows, cols, Inches(0.9), Inches(2.3), Inches(11.5), Inches(1.5)).table
    headers = ["Вход диагностики", "dominant_field_accuracy", "formula_hit_rate", "gap_sanity"]
    data = [["3 пика (как в проде)", "0.667", "1.0", "1.0"],
            ["полный набор черт (ablation)", "1.000", "1.0", "1.0"]]
    for j, h in enumerate(headers):
        c = tbl.cell(0, j)
        c.text = h
        for p in c.text_frame.paragraphs:
            for r in p.runs:
                r.font.size, r.font.name, r.font.bold = Pt(12), SANS, True
                r.font.color.rgb = CREAM
        c.fill.solid()
        c.fill.fore_color.rgb = WINE
    for i, row in enumerate(data, start=1):
        for j, val in enumerate(row):
            c = tbl.cell(i, j)
            c.text = val
            for p in c.text_frame.paragraphs:
                for r in p.runs:
                    r.font.size, r.font.name = Pt(13), SANS
                    r.font.color.rgb = INK
                    r.font.bold = (i == 2 and j == 1)
            c.fill.solid()
            c.fill.fore_color.rgb = CREAM

    _text(s, 0.9, 4.2, 11.5, 1.0,
          "Инсайт из данных: продукт обрезает желаемые черты до трёх и теряет сигнал —\n"
          "на полном наборе попадание в поле эксперта растёт с 2/3 до 3/3.",
          18, SANS, INK)
    _text(s, 0.9, 5.5, 11.5, 0.8,
          "Честно: выборка мала (n = 3). Vision изолирован, чтобы мерить именно диагностику.\n"
          "Метрику расширяем по мере накопления кейсов.",
          14, SERIF, WINE, italic=True)
    _notes(s, "3:15–4:00\n\n«Мы оцениваем диагностику против экспертной разметки. Vision изолируем, чтобы "
              "мерить именно диагноз. И сразу получили инсайт из данных: продукт обрезает желаемые черты "
              "до трёх и теряет сигнал — на полном наборе точность попадания в поле эксперта растёт "
              "с 2/3 до 3/3. Оговорюсь честно: выборка пока мала, n=3, метрику расширяем по мере "
              "накопления кейсов.»\n\nНЕ проговаривать все числа — показать таблицу, назвать один вывод.")

    # ── 7. Результаты ──────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "06 · Результаты и импакт")
    _text(s, 0.9, 1.15, 8.6, 0.9, "MVP работает end-to-end —\nна реальных клиентках", 30, SERIF, INK)
    # Числа — только по живым клиенткам: смоук-тесты и самотесты автора из воронки исключены
    # (иначе меряем свою же работу; жюри разбирает такую конверсию первым вопросом).
    kpis = [("8", "прохождений\nквиза"), ("4", "клиентки\nс замером Gap"),
            ("66 с", "полная Карта\nс образами"), ("92", "теста,\nCI зелёный")]
    x = 0.9
    for val, sub in kpis:
        _text(s, x, 2.55, 2.0, 0.8, val, 36, SERIF, WINE)
        _text(s, x, 3.45, 2.0, 0.8, sub, 12, SANS, MUTED)
        x += 2.15
    _bullets(s, [
        "Публичный репозиторий, Docker, CI со сборкой образа и smoke-тестом",
        "Панель метрик: воронка, Gap, отзывы — данные, а не ощущения",
        "Разброс Gap 50–78%: шкала различает состояния, а не константа",
        "Из воронки исключены смоук-тесты и самотесты автора — считаем спрос",
    ], top=4.4, size=13, gap=0.42, width=7.4)
    _text(s, 0.9, 6.15, 7.9, 0.9,
          "Ценность меряем не «понравилось», а процентом закрытия Identity Gap.\nЛонгитюд «до/после» набираем: эффект от ношения требует недель.",
          12, SERIF, WINE, italic=True)
    _pair_photos(s, left=8.9, top=1.9, height=4.0, caption_size=10)
    _notes(s, "4:00–4:40\n\n«MVP работает end-to-end: полная Карта за минуту. Инженерия закрыта — публичный "
              "репозиторий, Docker, CI со сборкой образа и smoke-тестом, 92 теста. Ценность измеряем не "
              "«понравилось», а процентом закрытия Identity Gap.»\n\n"
              "ЧИСЛА ЧЕСТНЫЕ: 8 прохождений и 4 замера — это ЖИВЫЕ клиентки. Смоук-тесты и самотесты "
              "автора из воронки исключены (раньше метрика показывала 12 и средний Gap 69.8%, раздутый "
              "тестами; честный — 62%). Если спросят «почему так мало» — ответ: «потому что мы не "
              "считаем свою же работу как спрос».\n\n"
              "ВАЖНО про «до/после»: НЕ заявлять, что трансформация измерена. Данных лонгитюда нет — оба "
              "повторных замера сделаны в один день (шум), это видно в /metrics отдельной строкой. "
              "Честная формулировка: «механика замера вшита в продукт, инфраструктура готова, лонгитюд "
              "требует недель — это следующий шаг». Работает так же, как оговорка про n=3.\n\n"
              "ВИЗУАЛ: кадр «до/после» повторяет титул.")

    # ── 8. Мотивация ───────────────────────────────────────────────────────────
    s = prs.slides.add_slide(blank)
    _bg(s, prs)
    _eyebrow(s, "07 · Мотивация")
    _text(s, 0.9, 1.5, 11.5, 1.6,
          "Я построила эту систему как доменный эксперт,\nкоторый сам собрал AI-слой.",
          32, SERIF, INK)
    _bullets(s, [
        "15+ лет в авиации: инженерно-навигационные расчёты — привычка к точности",
        "Имидж-консалтинг: авторская методология, валидированная профильными экспертами",
        "AI Talent Hub — углубить инженерию и Data Science",
        "Цель: довести оценку и генерацию до продакшн-качества",
    ], top=3.6, size=17, gap=0.62)
    _text(s, 0.9, 6.3, 11.5, 0.6, "Спасибо.", 24, SERIF, WINE, italic=True)
    _notes(s, "4:40–5:00\n\n«Я построила эту систему как доменный эксперт, который сам собрал AI-слой. "
              "Хочу углубить инженерию и Data Science в AITH, чтобы довести оценку и генерацию до "
              "продакшн-качества. Спасибо.»\n\nДержать 5:00 — на вопросы отдельные 7 минут "
              "(см. 03-сценарий-защиты.md).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    a = ap.parse_args()

    prs = Presentation()
    prs.slide_width = Inches(13.333)   # 16:9
    prs.slide_height = Inches(7.5)
    build(prs)

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    # маркеры ASCII: консоль Windows в cp1251 роняет «✓»/«→» уже ПОСЛЕ записи файла
    print(f"OK: {len(prs.slides.__iter__.__self__._sldIdLst)} слайдов -> {out.relative_to(ROOT)}")
    print("Открой в PowerPoint: правь текст, вставь визуалы «до/после» на слайды 1 и 7,")
    print("затем Файл -> Сохранить как -> PDF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
