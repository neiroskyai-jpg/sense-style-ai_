"""Кеш образов: не генерируем дважды одно и то же — и не путаем клиенток.

Одна Карта стиля — 8 обращений к image-модели. Без кеша пересборка Карты и повторный прогон
демо сжигали ключ заново. Но кеш по «состав образа + сезон» без личности вернул бы одной
клиентке кадр другой, поэтому отпечаток фото обязателен в ключе.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from core import imgcache  # noqa: E402
from core import pipeline as p  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(imgcache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(imgcache, "ENABLED", True)
    calls = {"n": 0}

    def fake_gen(instruction, model=None, ref_images=None):
        calls["n"] += 1
        return [f"data:image/png;base64,IMG{calls['n']}"]

    monkeypatch.setattr(p.provider, "generate_image", fake_gen)
    monkeypatch.setattr(p.provider, "encode_image", lambda *a, **k: "BODY")
    monkeypatch.setattr(p.provider, "head_crop", lambda *a, **k: "FACE")

    anna = tmp_path / "anna.jpg"; anna.write_bytes(b"photo-of-anna")
    maria = tmp_path / "maria.jpg"; maria.write_bytes(b"photo-of-maria")
    return {"calls": calls, "anna": str(anna), "maria": str(maria)}


def test_same_look_is_generated_once(env):
    """Главный смысл: повтор не стоит ни одной генерации."""
    first = p.render_look_on_client(env["anna"], "wool coat", season="winter")
    second = p.render_look_on_client(env["anna"], "wool coat", season="winter")

    assert first == second
    assert env["calls"]["n"] == 1


def test_another_client_never_gets_someone_elses_frame(env):
    """Критично: кадр привязан к лицу. Совпадение ключа возможно только у человека с самим собой."""
    anna = p.render_look_on_client(env["anna"], "wool coat", season="winter")
    maria = p.render_look_on_client(env["maria"], "wool coat", season="winter")

    assert anna != maria
    assert env["calls"]["n"] == 2


def test_season_and_outfit_change_the_frame(env):
    """Промпт и сезон входят в ключ: правка образа обязана давать новый кадр, а не старый из кеша."""
    base = p.render_look_on_client(env["anna"], "wool coat", season="winter")
    other_season = p.render_look_on_client(env["anna"], "wool coat", season="summer")
    other_look = p.render_look_on_client(env["anna"], "linen dress", season="winter")

    assert len({base, other_season, other_look}) == 3
    assert env["calls"]["n"] == 3


def test_same_photo_uploaded_twice_still_hits(env, tmp_path):
    """Отпечаток по содержимому, а не по имени: повторная загрузка того же фото ложится в тот же ключ."""
    p.render_look_on_client(env["anna"], "wool coat", season="winter")
    copy = tmp_path / "anna-copy.jpg"
    copy.write_bytes(Path(env["anna"]).read_bytes())

    p.render_look_on_client(str(copy), "wool coat", season="winter")

    assert env["calls"]["n"] == 1, "то же фото под другим именем не должно стоить генерации"


def test_failed_generation_is_not_cached(env, monkeypatch):
    """Пустой ответ модели нельзя запоминать — иначе сбой закрепится навсегда."""
    monkeypatch.setattr(p.provider, "generate_image", lambda *a, **k: [""])
    p.render_look_on_client(env["anna"], "wool coat", season="winter")

    assert imgcache.stats()["entries"] == 0


def test_cache_can_be_switched_off(env, monkeypatch):
    """Выключатель для отладки генерации: каждый прогон должен идти в модель."""
    monkeypatch.setattr(imgcache, "ENABLED", False)

    p.render_look_on_client(env["anna"], "wool coat", season="winter")
    p.render_look_on_client(env["anna"], "wool coat", season="winter")

    assert env["calls"]["n"] == 2


def test_eviction_keeps_cache_bounded(monkeypatch, tmp_path):
    """Кадры весят ~1 МБ: без предела кеш съел бы диск контейнера."""
    monkeypatch.setattr(imgcache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(imgcache, "ENABLED", True)
    monkeypatch.setattr(imgcache, "MAX_ENTRIES", 3)

    for i in range(6):
        imgcache.put(f"key{i}", f"data:image/png;base64,X{i}")

    assert imgcache.stats()["entries"] <= 3
