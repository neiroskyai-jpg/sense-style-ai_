"""Погода по городу клиентки — для «образа дня» в кабинете.

Совет по одежде должен опираться на реальную погоду, иначе «надень жакет» звучит одинаково
в +25 и в −10. Источник — OpenWeatherMap (бесплатный тариф), ключ в `OPENWEATHER_API_KEY`.

Без ключа или при сбое сети модуль возвращает None — кабинет тогда просто не показывает блок
погоды. Погода никогда не должна ронять кабинет.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

API_URL = "https://api.openweathermap.org/data/2.5/weather"
_TIMEOUT = 6
_CACHE_TTL = 30 * 60  # полчаса: погода меняется медленнее, чем клиентка обновляет страницу
_cache: dict[str, tuple[float, dict]] = {}


def configured() -> bool:
    return bool(os.getenv("OPENWEATHER_API_KEY"))


def get_weather(city: str) -> Optional[dict]:
    """Погода в городе: {city, temp, feels_like, description, icon, wind, is_rain, is_snow}.

    None — если города нет, ключ не задан или сервис недоступен.
    """
    city = (city or "").strip()
    key = os.getenv("OPENWEATHER_API_KEY")
    if not city or not key:
        return None

    cached = _cache.get(city.lower())
    if cached and time.time() - cached[0] < _CACHE_TTL:
        return cached[1]

    try:
        r = requests.get(API_URL, timeout=_TIMEOUT, params={
            "q": city, "appid": key, "units": "metric", "lang": "ru",
        })
        if r.status_code != 200:
            return None
        d = r.json()
    except Exception:  # noqa: BLE001 — погода не должна ронять кабинет
        return None

    main = d.get("main") or {}
    weather = (d.get("weather") or [{}])[0]
    code = str(weather.get("id") or "")
    out = {
        "city": d.get("name") or city,
        "temp": round(main.get("temp", 0)),
        "feels_like": round(main.get("feels_like", main.get("temp", 0))),
        "description": (weather.get("description") or "").capitalize(),
        "icon": weather.get("icon") or "",
        "wind": round((d.get("wind") or {}).get("speed", 0)),
        "is_rain": code.startswith(("2", "3", "5")),   # гроза, морось, дождь
        "is_snow": code.startswith("6"),
    }
    _cache[city.lower()] = (time.time(), out)
    return out


# Пороги по ощущаемой температуре. Опираемся на feels_like, а не на градусник: одеваемся по
# ощущению, и при ветре +10 требует того же, что тихие +5.
def dress_advice(w: dict) -> dict:
    """Совет по одежде под погоду: {layer, note, tags}.

    layer — что добавить поверх капсулы, note — человеческая формулировка,
    tags — ключевые слова для подсветки вещей капсулы (верхний слой, обувь).
    """
    if not w:
        return {}
    t = w.get("feels_like", w.get("temp", 0))
    rain, snow, wind = w.get("is_rain"), w.get("is_snow"), w.get("wind", 0)

    if t >= 22:
        layer, tags = "без верхнего слоя", ["лёгкий верх", "открытая обувь"]
        note = "Жарко — капсула работает без верхнего слоя. Бери лёгкие ткани и светлые вещи из палитры."
    elif t >= 15:
        layer, tags = "лёгкий жакет или кардиган", ["жакет", "кардиган", "лоферы"]
        note = "Тепло, но не жарко — жакет или кардиган держит собранность и не перегревает."
    elif t >= 7:
        layer, tags = "тренч или плотный жакет", ["тренч", "жакет", "ботинки"]
        note = "Прохладно — нужен плотный верхний слой. Это как раз та погода, где тренч из капсулы работает лучше всего."
    elif t >= -2:
        layer, tags = "пальто", ["пальто", "ботинки", "шарф"]
        note = "Холодно — пальто и закрытая обувь. Шарф из палитры добавит цвет там, где его не хватает зимой."
    else:
        layer, tags = "тёплое пальто или пуховик", ["пальто", "пуховик", "сапоги", "шарф"]
        note = "Мороз — тепло важнее образа, но силуэт держим: длинное пальто вместо короткой куртки."

    if snow:
        note += " Снег — обувь на устойчивой подошве."
        tags.append("сапоги")
    elif rain:
        note += " Дождь — плащ и обувь, которую не жалко."
        tags.append("плащ")
    if wind >= 8:
        note += " Ветрено — верхний слой лучше застёгивать."

    return {"layer": layer, "note": note, "tags": tags}
