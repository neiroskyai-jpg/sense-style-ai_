"""Тесты измерительного цветотипа: классификация по цвету кожи + детект кожи + Lab.

Точные пороги сезонов будут калиброваться на реальных фото — здесь проверяем,
что НАПРАВЛЕНИЕ измерений верное (тёплый/холодный, светлый/тёмный) и математика не врёт.
"""
import io

from PIL import Image

from core.colortype import (analyze_colortype, classify, _rgb_to_lab,
                            _skin_pixels)


def test_lab_known_values():
    # Белый → L≈100, a≈0, b≈0; чёрный → L≈0
    Lw, aw, bw = _rgb_to_lab((255, 255, 255))
    assert Lw > 99 and abs(aw) < 1 and abs(bw) < 1
    Lk, _, _ = _rgb_to_lab((0, 0, 0))
    assert Lk < 1


def test_warm_vs_cool_undertone():
    # Тёплая золотистая кожа vs холодная розоватая
    warm = classify((215, 165, 120))     # золотисто-загорелая
    cool = classify((220, 175, 175))     # розовато-холодная
    assert warm.undertone == "warm"
    assert cool.undertone in ("cool", "neutral")
    assert warm.measurements["b"] > cool.measurements["b"]  # больше жёлтого у тёплой


def test_value_light_vs_deep():
    light = classify((240, 215, 195))    # очень светлая кожа
    deep = classify((120, 85, 60))       # тёмная кожа
    assert light.value == "light"
    assert deep.value == "deep"
    assert light.measurements["L"] > deep.measurements["L"]


def test_season_mapping():
    # тёплый+светлый → spring; холодный+тёмный → winter
    assert classify((235, 200, 150)).season in ("spring", "autumn")
    cool_deep = classify((110, 80, 85))
    assert cool_deep.season in ("winter", "summer")


def test_skin_detection_picks_skin_over_background():
    pix = [(210, 160, 120)] * 50 + [(20, 60, 200)] * 50  # кожа + синий фон
    skin = _skin_pixels(pix)
    assert len(skin) >= 40
    assert all(p[2] < 160 for p in skin)  # синий фон не попал


def test_analyze_on_synthetic_image():
    # Сплошной «кожаный» кадр → пайплайн отрабатывает и отдаёт сырьё
    img = Image.new("RGB", (64, 64), (210, 165, 120))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    res = analyze_colortype(buf, white_balance=False)
    assert res.colortype.count("_") == 1
    assert "L" in res.measurements and "hue" in res.measurements
    assert 0.0 <= res.confidence <= 1.0
