"""Бесплатная Карта не сжигает ключ, а демо фаундера не режется вместе с гостями.

Полная Карта — 17 обращений к image-модели: 6 образов + 2 стилизации, раскладка на каждый из
них и раскладка капсулы. Платного тарифа в коде пока нет, поэтому столько получал КАЖДЫЙ
зашедший. 22.07.2026 на этом кончился баланс провайдера: продукт встал целиком — ни
диагностики, ни Карты, ни превью после квиза, — а ссылка на живой продукт стоит в конкурсной
заявке.

Здесь сторожим три вещи: бесплатному посетителю рисуется ограниченное число образов, раскладки
не генерируются для образов без картинки, а подтверждённый админ получает Карту целиком.
"""
from pathlib import Path

import pytest

import app.main as m


@pytest.fixture
def rendered(monkeypatch):
    """Считает обращения к image-модели вместо настоящих вызовов."""
    calls = {"looks": [], "flatlays": [], "capsule": 0}

    def _look(photo, prompt, season=None, **kw):
        calls["looks"].append(prompt)
        return "data:image/png;base64,LOOK"

    def _flat(items, palette="", season=None, **kw):
        calls["flatlays"].append(tuple(items or ()))
        return "data:image/png;base64,FLAT"

    def _capsule(items, palette="", season=None, **kw):
        calls["capsule"] += 1
        return "data:image/png;base64,CAPS"

    monkeypatch.setattr(m, "render_look_on_client", _look)
    monkeypatch.setattr(m, "render_flatlay", _flat)
    monkeypatch.setattr(m, "render_capsule_flatlay", _capsule)
    return calls


@pytest.fixture
def card(monkeypatch, tmp_path):
    """Карта с 6 образами и 2 стилизациями — как в проде."""
    looks = [{"scenario": f"сценарий {i}", "items": [f"вещь {i}"]} for i in range(6)]
    styling = [{"scenario": f"стилизация {i}", "items": [f"вещь s{i}"]} for i in range(2)]
    built = {"looks": looks, "styling": {"looks": styling}, "season": "fw", "palette": []}
    monkeypatch.setattr(m, "build_style_card", lambda diag, season=None: built)
    monkeypatch.setattr(m, "get_profile", lambda email: {"diagnosis": {"style_formula": "Классика"}})
    monkeypatch.setattr(m, "save_card", lambda *a, **k: None)
    monkeypatch.setattr(m, "save_diagnosis", lambda *a, **k: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "capsule_items_from_looks", lambda looks: ["вещь 0"])
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"jpeg")
    return photo


def _build(photo, full_render):
    job = "job-test"
    m._JOBS[job] = {"status": "processing"}
    m._card_job_worker(job, photo, "client@test.ru", "fw", full_render)
    return m._JOBS[job]


def test_free_visitor_gets_only_the_budgeted_looks(rendered, card, monkeypatch):
    """Гость получает SENSE_FREE_LOOKS образов, а не все восемь."""
    monkeypatch.setattr(m, "FREE_LOOKS", 2)

    _build(card, full_render=False)

    assert len(rendered["looks"]) == 2, "бесплатная Карта не должна рисовать все образы"


def test_admin_gets_the_whole_card(rendered, card, monkeypatch):
    """Демо на защите и прогоны на клиентках лимитом не задеваются."""
    monkeypatch.setattr(m, "FREE_LOOKS", 2)

    _build(card, full_render=True)

    assert len(rendered["looks"]) == 8, "у подтверждённого админа Карта собирается целиком"


def test_flatlays_follow_the_looks_that_were_drawn(rendered, card, monkeypatch):
    """Раскладка стоит столько же, сколько образ, — рисуем её только там, где образ есть.

    Раньше раскладки собирались по всем восьми образам независимо от того, отрисовались ли они.
    """
    monkeypatch.setattr(m, "FREE_LOOKS", 2)

    _build(card, full_render=False)

    assert len(rendered["flatlays"]) == 2


def test_total_image_calls_drop_by_an_order(rendered, card, monkeypatch):
    """Главное число: сколько обращений к платной модели стоит бесплатный посетитель."""
    monkeypatch.setattr(m, "FREE_LOOKS", 2)

    _build(card, full_render=False)
    free = len(rendered["looks"]) + len(rendered["flatlays"]) + rendered["capsule"]

    for key in ("looks", "flatlays"):
        rendered[key].clear()
    rendered["capsule"] = 0
    _build(card, full_render=True)
    full = len(rendered["looks"]) + len(rendered["flatlays"]) + rendered["capsule"]

    assert full == 17, "полная Карта — 17 обращений; если изменилось, пересчитай экономику"
    assert free <= 5, f"бесплатная Карта всё ещё дорогая: {free} обращений"


def test_text_card_stays_whole_for_free_visitors(rendered, card, monkeypatch):
    """Режем только картинки: описания всех образов клиентка видит полностью."""
    monkeypatch.setattr(m, "FREE_LOOKS", 2)

    _build(card, full_render=False)
    saved = m.build_style_card({}, season="fw")

    assert len(saved["looks"]) == 6, "текстовая часть Карты не должна усекаться"
    assert len(saved["styling"]["looks"]) == 2
