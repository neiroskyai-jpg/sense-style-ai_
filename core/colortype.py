"""Измерительный цветотип: фото → пробы кожи → Lab → 12 сезонов (без «LLM на глаз»).

Зачем: vision-модель путает Лето↔Осень из-за тёплого света. Здесь — ИЗМЕРЕНИЕ:
находим пиксели кожи, корректируем баланс белого, считаем подтон/светлоту/насыщенность
в Lab и классифицируем. Выход объясним: «подтон тёплый, светлота средняя».

STANDALONE: не импортируется в пайплайн/веб, пока не откалибруем на реальных фото.
Только Pillow + stdlib — без numpy/opencv, чтобы не трогать сборку Amvera.

ВАЖНО: пороги классификации — стартовые, требуют калибровки на размеченных фото
(в идеале — на дипломных кейсах Ксении). Поэтому функция всегда возвращает сырые
измерения (Lab, подтон, светлота, насыщенность), чтобы пороги можно было подкрутить.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class ColortypeResult:
    season: str            # spring | summer | autumn | winter
    subtype: str           # light | natural | contrast
    colortype: str         # напр. "summer_natural" (как в движке)
    undertone: str         # warm | cool | neutral
    value: str             # light | medium | deep
    chroma: str            # clear | muted
    measurements: dict     # сырьё: skin_rgb, L, a, b, hue, chroma_c, skin_px
    confidence: float      # 0..1 — груба: доля найденных пикселей кожи

    def as_dict(self) -> dict:
        return {
            "season": self.season, "subtype": self.subtype, "colortype": self.colortype,
            "undertone": self.undertone, "value": self.value, "chroma": self.chroma,
            "measurements": self.measurements, "confidence": round(self.confidence, 3),
        }


# ── Публичное API ─────────────────────────────────────────────────────────────

def analyze_colortype(image_path: str | Path, white_balance: bool = True) -> ColortypeResult:
    """Фото → ColortypeResult по методу photo-reading.md: температура (кожа) → сезон,
    КОНТРАСТ (кожа vs волосы по 11-шкале) → подтип. white_balance — коррекция засвета."""
    img = Image.open(image_path).convert("RGB")
    img.thumbnail((256, 256))            # ускоряем: пиксельная математика на чистом Python
    w, h = img.size
    data = img.tobytes()                 # RGB-байты подряд (без deprecated getdata)
    pixels = [(data[i], data[i + 1], data[i + 2]) for i in range(0, len(data), 3)]

    if white_balance:
        pixels = _white_patch(pixels)

    skin = _skin_pixels(pixels)
    skin_px = len(skin)
    total = max(1, len(pixels))
    if skin_px < 20:                     # кожи почти не нашли — низкая уверенность, берём всё
        skin = pixels
    skin_rgb = _median_rgb(skin)

    # Волосы: тёмная неъкожная зона в верхней части кадра (грубая оценка без сегментации)
    hair_rgb = _hair_rgb(pixels, w, h)

    return classify(skin_rgb, hair_rgb=hair_rgb,
                    confidence=min(1.0, skin_px / (total * 0.12)))


def classify(skin_rgb: tuple[int, int, int],
             hair_rgb: tuple[int, int, int] | None = None,
             confidence: float = 1.0) -> ColortypeResult:
    """Классификация по методу: сезон — из температуры+светлоты кожи; подтип — из КОНТРАСТА
    (светлота кожи vs волос по 11-ступенчатой шкале). Тестируется без фото."""
    L, a, b = _rgb_to_lab(skin_rgb)
    hue = math.degrees(math.atan2(b, a)) if (a or b) else 0.0
    chroma_c = math.hypot(a, b)

    # Подтон: тёплый = больше жёлтого относительно красного (высокий hue к 70-90°).
    if hue >= 57:
        undertone = "warm"
    elif hue <= 47:
        undertone = "cool"
    else:
        undertone = "neutral"

    # Светлота кожи по L*.
    value = "light" if L >= 68 else ("deep" if L <= 56 else "medium")

    # Контраст внешности (ПРАВИЛО ПРИОРИТЕТА photo-reading.md: контраст важнее таблицы).
    # 11-ступенчатая ахроматическая шкала: разница делений кожа↔волосы → уровень → подтип.
    contrast_steps = None
    if hair_rgb is not None:
        L_hair = _rgb_to_lab(hair_rgb)[0]
        contrast_steps = abs(_l_step(L) - _l_step(L_hair))
        contrast_level = _contrast_level(contrast_steps)
    else:
        contrast_level = None

    chroma = "clear" if chroma_c >= 28 else "muted"
    season = _season(undertone, value)
    subtype = _subtype(contrast_level, value, chroma)

    meas = {
        "skin_rgb": list(skin_rgb), "L": round(L, 1), "a": round(a, 1),
        "b": round(b, 1), "hue": round(hue, 1), "chroma_c": round(chroma_c, 1),
    }
    if hair_rgb is not None:
        meas.update({"hair_rgb": list(hair_rgb), "L_hair": round(_rgb_to_lab(hair_rgb)[0], 1),
                     "contrast_steps": contrast_steps, "contrast_level": contrast_level})

    return ColortypeResult(
        season=season, subtype=subtype, colortype=f"{season}_{subtype}",
        undertone=undertone, value=value, chroma=chroma,
        measurements=meas, confidence=confidence,
    )


def _l_step(L: float) -> int:
    """L* (0..100) → деление ахроматической шкалы 1 (чёрный) .. 11 (белый)."""
    return max(1, min(11, round(L / 100 * 10) + 1))


def _contrast_level(steps: int) -> str:
    """Разница делений → уровень контраста (пороги из photo-reading.md, Шаг 2)."""
    if steps >= 7:
        return "high"
    if steps >= 4:
        return "medium"
    return "low"


def _season(undertone: str, value: str) -> str:
    warm = undertone == "warm"
    light = value == "light"
    if warm and light:
        return "spring"
    if warm and not light:
        return "autumn"
    if (not warm) and light:
        return "summer"
    return "winter"  # cool + deep/medium


def _subtype(contrast_level: str | None, value: str, chroma: str) -> str:
    """Подтип по КОНТРАСТУ (приоритет метода): high→contrast, medium→natural, low→light.
    Без замера волос — фолбэк на старую логику (chroma/светлота)."""
    if contrast_level == "high":
        return "contrast"
    if contrast_level == "medium":
        return "natural"
    if contrast_level == "low":
        return "light"
    # фолбэк, если волосы не считались
    if chroma == "clear":
        return "contrast"
    return "light" if value == "light" else "natural"


def _hair_rgb(pixels: list[tuple[int, int, int]], w: int, h: int) -> tuple[int, int, int] | None:
    """Грубая оценка цвета волос: тёмные неъкожные пиксели в верхних ~45% кадра.

    Без сегментации лица — эвристика. Отсекаем кожу и почти-чёрный фон/тени (L<8).
    Берём медиану самой тёмной трети найденного. None — если кандидатов мало.
    """
    top_rows = int(h * 0.45)
    cand = []
    for idx, px in enumerate(pixels):
        if idx // w >= top_rows:
            break
        if _is_skin(px):
            continue
        L = _rgb_to_lab(px)[0]
        if 8 <= L <= 60:                 # тёмное, но не угольный фон
            cand.append((L, px))
    if len(cand) < 15:
        return None
    cand.sort(key=lambda t: t[0])        # от тёмного к светлому
    darkest = [px for _, px in cand[: max(15, len(cand) // 3)]]
    return _median_rgb(darkest)


# ── Пиксельная математика (чистый Python) ─────────────────────────────────────

def _white_patch(pixels: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """White-patch баланс белого: тянем 97-й перцентиль каждого канала к 255.

    Грубо снимает цветной засвет (тёплая лампа), сохраняя относительный подтон кожи.
    """
    rs = sorted(p[0] for p in pixels)
    gs = sorted(p[1] for p in pixels)
    bs = sorted(p[2] for p in pixels)
    idx = int(len(pixels) * 0.97) - 1
    idx = max(0, min(idx, len(pixels) - 1))
    rm, gm, bm = max(1, rs[idx]), max(1, gs[idx]), max(1, bs[idx])
    sr, sg, sb = 255 / rm, 255 / gm, 255 / bm
    out = []
    for r, g, b in pixels:
        out.append((min(255, int(r * sr)), min(255, int(g * sg)), min(255, int(b * sb))))
    return out


def _is_skin(px: tuple[int, int, int]) -> bool:
    """Пиксель похож на кожу по YCbCr (Cr∈[133,173], Cb∈[77,127])."""
    r, g, b = px
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b
    cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b
    return 80 <= y <= 245 and 77 <= cb <= 127 and 133 <= cr <= 173


def _skin_pixels(pixels: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Отбор пикселей кожи по YCbCr (классический диапазон Cr∈[133,173], Cb∈[77,127])."""
    return [px for px in pixels if _is_skin(px)]


def _median_rgb(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not pixels:
        return (0, 0, 0)
    n = len(pixels)
    mid = n // 2
    return tuple(sorted(p[i] for p in pixels)[mid] for i in range(3))  # type: ignore


def _rgb_to_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0..255) → CIELab (D65). Стандартные формулы, без numpy."""
    def _lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_lin(rgb[0]), _lin(rgb[1]), _lin(rgb[2]))
    # sRGB → XYZ (D65)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) / 0.95047
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) / 1.0
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) / 1.08883

    def _f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)

    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
