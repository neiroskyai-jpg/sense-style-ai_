"""Калибровка измерительного цветотипа на размеченных фото.

Складывай фото в папки по ИЗВЕСТНОМУ сезону (имя папки = метка):
    data/colortype-calibration/autumn_natural/foto1.jpg
    data/colortype-calibration/summer_natural/foto2.jpg
    ...
Имя папки — это либо сезон (spring/summer/autumn/winter), либо полный цветотип
(summer_natural). Скрипт прогоняет пайплайн и показывает предсказание vs метку
плюс сырые измерения (L, hue, подтон) — по ним крутим пороги в core/colortype.py.

Запуск:  python scripts/calibrate_colortype.py

ВАЖНО (приватность): это персональные фото — НЕ коммить их в git. Папка в .gitignore.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.colortype import analyze_colortype  # noqa: E402

CAL_DIR = Path(__file__).resolve().parent.parent / "data" / "colortype-calibration"
EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SEASONS = ("spring", "summer", "autumn", "winter")


def _true_season(label: str) -> str:
    label = label.lower()
    for s in SEASONS:
        if label.startswith(s):
            return s
    return label


def main() -> None:
    if not CAL_DIR.exists():
        CAL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Создал папку: {CAL_DIR}")
        print("Положи фото в подпапки по сезону, напр. autumn_natural/foto.jpg, и запусти снова.")
        return

    rows, season_ok, undertone_ok, total = [], 0, 0, 0
    for sub in sorted(CAL_DIR.iterdir()):
        if not sub.is_dir():
            continue
        true_full = sub.name.lower()
        true_season = _true_season(true_full)
        for img in sorted(sub.iterdir()):
            if img.suffix.lower() not in EXTS:
                continue
            try:
                r = analyze_colortype(img)
            except Exception as e:  # noqa: BLE001
                print(f"[ошибка] {img.name}: {e}")
                continue
            total += 1
            s_ok = (r.season == true_season)
            season_ok += s_ok
            m = r.measurements
            rows.append((img.name[:22], true_full, r.colortype, r.undertone,
                         r.value, r.chroma, m["L"], m["hue"], "✓" if s_ok else "✗"))

    if not total:
        print(f"Фото не найдено в {CAL_DIR}. Положи их в подпапки по сезону.")
        return

    hdr = ("файл", "метка", "предсказание", "подтон", "светл.", "насыщ.", "L", "hue", "сезон?")
    print(f"\n{'':-<96}")
    print("{:<22} {:<16} {:<16} {:<7} {:<7} {:<6} {:<6} {:<6} {}".format(*hdr))
    print(f"{'':-<96}")
    for r in rows:
        print("{:<22} {:<16} {:<16} {:<7} {:<7} {:<6} {:<6} {:<6} {}".format(*r))
    print(f"{'':-<96}")
    print(f"Сезон угадан: {season_ok}/{total} ({100*season_ok//total}%). "
          f"Это база для подкрутки порогов undertone/value/chroma в core/colortype.py.")


if __name__ == "__main__":
    main()
