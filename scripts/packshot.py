"""Выбор предметного кадра (packshot) из галереи товара.

Зачем: бренды-editorial (Lichi, Ushatava) первым кадром ставят съёмку НА МОДЕЛИ. В капсуле нужна
сама вещь — «пиджак = пиджак», без девушки и лишних вещей в кадре. В галерее предметный кадр обычно
есть (у Lichi это кадры [1]/[2]: вещь на сером фоне), но не всегда — у аксессуаров его может не быть.

Как решаем: качаем кадры, считаем долю «кожи» (модель в кадре) и однородность фона (предметная
съёмка — ровный светлый фон). Берём кадр с наименьшей долей кожи; при равенстве — с более ровным
фоном. Если предметного кадра нет (все кадры с моделью) — честно возвращаем первый, вещь всё равно
показывается, просто на модели.
"""
from __future__ import annotations

import io

import requests
from PIL import Image

_UA = {"User-Agent": "Mozilla/5.0"}
_SKIN_MAX = 0.04   # >4% пикселей кожи → в кадре есть модель
_BG_MAX = 8.0      # разброс яркости фона: у предметной съёмки почти 0, у студийного кадра 50–100
_SAMPLE = 160      # ресайз для анализа: считать по мелкой картинке достаточно и быстро


def _skin_fraction(img: Image.Image) -> float:
    """Доля пикселей телесного оттенка. Классическое RGB-правило детекции кожи."""
    px = list(img.getdata())
    if not px:
        return 1.0
    skin = 0
    for r, g, b in px:
        if r > 95 and g > 40 and b > 20 and (max(r, g, b) - min(r, g, b)) > 15 and r > g and r > b:
            skin += 1
    return skin / len(px)


def _bg_uniformity(img: Image.Image) -> float:
    """Разброс яркости по рамке кадра. Предметная съёмка = ровный фон → маленький разброс."""
    w, h = img.size
    border = [img.getpixel((x, y))
              for x in range(0, w, 4) for y in (0, h - 1)] + \
             [img.getpixel((x, y))
              for y in range(0, h, 4) for x in (0, w - 1)]
    if not border:
        return 999.0
    lum = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in border]
    mean = sum(lum) / len(lum)
    return (sum((v - mean) ** 2 for v in lum) / len(lum)) ** 0.5


def score_frame(url: str, timeout: float = 20.0) -> tuple[float, float] | None:
    """(доля кожи, разброс фона) для одного кадра. None — если кадр не скачался."""
    try:
        r = requests.get(url, headers=_UA, timeout=timeout)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((_SAMPLE, _SAMPLE))
        return _skin_fraction(img), _bg_uniformity(img)
    except Exception:  # noqa: BLE001 — кадр недоступен → просто не рассматриваем
        return None


def pick_packshot(urls: list[str], timeout: float = 20.0) -> tuple[str, bool]:
    """Из галереи выбрать предметный кадр. Возвращает (url, это_packshot).

    Предметный кадр = мало кожи И плоский фон. Одной «кожи» мало: студийный кадр тёмной вещи на
    модели без головы (Ushatava так снимает) даёт почти нулевую кожу и притворяется packshot —
    ловим его по фону, который у живой съёмки неровный.

    Предметного кадра нет — (первый кадр, False): вещь всё равно видно, просто на модели.
    """
    urls = [u for u in urls if u]
    if not urls:
        return "", False

    scored = []
    for u in urls:
        s = score_frame(u, timeout)
        if s:
            scored.append((s[0], s[1], u))
    if not scored:
        return urls[0], False

    packshots = [t for t in scored if t[0] <= _SKIN_MAX and t[1] <= _BG_MAX]
    if packshots:
        packshots.sort(key=lambda t: (t[0], t[1]))  # меньше кожи, ровнее фон
        return packshots[0][2], True
    return urls[0], False
