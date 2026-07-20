"""Квиз без фото обязан доводить до Карты.

Регрессия, найденная живым прогоном: job_id заводился только в /api/analyze, то есть только
вместе с фото. Клиентка, нажавшая «Пропустить · показать результат без фото», проходила все
14 вопросов, видела свой результат — и на кнопке «Получить полную Карту стиля» попадала на
«Сначала — диагностика». Весь квиз впустую.

Диагноз без фото беднее (нет цветотипа и фигуры), но воронка обязана вести вперёд.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402

PAYLOAD = {
    "now_traits": "спокойная,надёжная",
    "want_traits": "властная,элегантная,дорогая",
    "gap": 31,
    "direction": "classic",
}


@pytest.fixture
def client(monkeypatch):
    m.app.config["TESTING"] = True
    store: dict = {}

    monkeypatch.setattr(m, "get_profile", lambda e: store.get(e, {}))
    monkeypatch.setattr(m, "save_diagnosis",
                        lambda e, d: store.setdefault(e, {}).__setitem__("diagnosis", d))
    monkeypatch.setattr(m, "save_card", lambda e, c: store.setdefault(e, {}).__setitem__("card", c))
    monkeypatch.setattr(m, "current_card_by_season",
                        lambda e: {"fw": store[e]["card"]} if store.get(e, {}).get("card") else {})
    monkeypatch.setattr(m, "_visual_capsule", lambda *a, **k: [])
    monkeypatch.setattr(m, "_capsule_board", lambda items: [])
    monkeypatch.setattr(m, "gap_progress", lambda e: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "_card_stale", lambda prof: False)
    c = m.app.test_client()
    c.store = store
    return c


def _diagnose_unavailable(monkeypatch):
    """Провайдер недоступен — кончился ключ или лимит. Худший день для конкурса."""
    def _boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(m, "diagnose", _boom)


def test_quiz_without_photo_returns_job(client):
    r = client.post("/api/quiz-diagnosis", json=PAYLOAD)

    assert r.status_code == 200
    assert r.get_json()["job_id"]


def test_card_opens_after_quiz_without_photo(client, monkeypatch):
    """Главное: с полученным job_id /card собирает Карту, а не разворачивает на диагностику."""
    _diagnose_unavailable(monkeypatch)
    job = client.post("/api/quiz-diagnosis", json=PAYLOAD).get_json()["job_id"]

    html = client.get(f"/card?from_job={job}").get_data(as_text=True)

    assert "Сначала — диагностика" not in html


def test_gap_from_quiz_survives_provider_outage(client, monkeypatch):
    """Провайдер лежит — Gap всё равно тот, что клиентка увидела в квизе, а не выдуманный."""
    _diagnose_unavailable(monkeypatch)

    gap = client.post("/api/quiz-diagnosis", json=PAYLOAD).get_json()["gap"]

    assert gap == 31


def test_session_remembers_job_without_from_job_param(client, monkeypatch):
    """Тарифные кнопки ведут на /card без ?from_job= — диагноз должен найтись через сессию."""
    _diagnose_unavailable(monkeypatch)
    client.post("/api/quiz-diagnosis", json=PAYLOAD)

    html = client.get("/card").get_data(as_text=True)

    assert "Сначала — диагностика" not in html


def test_fallback_diag_speaks_catalog_language():
    """Каталог фильтрует по КОДУ стиля. С русским «Классика» словарь категорий возвращал пустоту,
    фильтр по формуле отключался — и в капсулу «Классики» падали кружево и юбки с воланами."""
    from core.catalog import _FORMULA_CATEGORIES

    diag = m._quiz_only_diag({"now_traits": [], "want_traits_top3": []}, 31, "classic")

    assert diag["base_style"] in _FORMULA_CATEGORIES
    assert _FORMULA_CATEGORIES[diag["base_style"]], "категории формулы не должны быть пустыми"
    assert diag["style_formula"] == "Классика", "человеку показываем русское название"


def test_fallback_diag_has_dominant_field():
    """Без semantic_field_distribution список доминант пуст и стилевого совпадения не происходит."""
    diag = m._quiz_only_diag({}, 31, "drama")

    dist = diag["semantic_field_distribution"]
    assert max(dist, key=dist.get) == "drama"


def test_unknown_direction_falls_back_to_classic():
    """Мусор с клиента не должен превращаться в пустую формулу."""
    assert m._quiz_only_diag({}, 31, "<script>")["base_style"] == "classic"


def test_direction_becomes_readable_formula(client, monkeypatch):
    """Клиентке нельзя показывать служебный код направления вместо названия стиля."""
    _diagnose_unavailable(monkeypatch)
    client.post("/api/quiz-diagnosis", json=PAYLOAD)

    html = client.get("/card").get_data(as_text=True)

    assert "classic" not in html.lower().split("<style")[0]
    assert "Классика" in html


def test_index_is_the_same_number_in_card_and_cabinet(client, monkeypatch):
    """Индекс = 100 − разрыв везде. Карта показывала 69%, кабинет — 31%: одна метрика, два числа."""
    uid = "anon-idx"
    monkeypatch.setattr(m, "_current_user", lambda: uid)
    client.store[uid] = {"diagnosis": m._quiz_only_diag({}, 31, "classic"),
                         "card": {"formula": "Классика", "gap": 31, "season": "fw"}}

    cabinet = client.get("/cabinet").get_data(as_text=True)

    assert ">69%<" in cabinet, "кабинет обязан показывать индекс, а не сырой разрыв"
    assert ">31%<" not in cabinet


def test_model_is_not_called_on_this_path(client, monkeypatch):
    """Модель здесь не зовём вовсе: без фото она выдавала 99% вместо показанных клиентке 31%,
    а синхронный вызов держал кнопку «Получить Карту» мёртвой ~20 секунд."""
    def _boom(*a, **k):
        raise AssertionError("diagnose не должен вызываться без фото")

    monkeypatch.setattr(m, "diagnose", _boom)

    assert client.post("/api/quiz-diagnosis", json=PAYLOAD).get_json()["gap"] == 31


def test_endpoint_answers_fast(client):
    """Ответ должен быть мгновенным — клиентка жмёт кнопку сразу, ждать нечего."""
    import time

    t0 = time.perf_counter()
    client.post("/api/quiz-diagnosis", json=PAYLOAD)

    assert time.perf_counter() - t0 < 1.0


def test_brand_names_are_not_rendered_in_card():
    """Партнёрство с брендами не согласовано — их названия в Карте не показываем.

    Вещь, фото и цена остаются: видно, что подобрано реальное. Но LICHI/USHATAVA на экране
    выглядели бы как согласованная витрина, которой нет.
    """
    assert "it.brand" not in m.STYLE_CARD
    assert "it.name" in m.STYLE_CARD, "сама вещь остаётся на месте"
    assert "it.price" in m.STYLE_CARD, "цена показывает, что подбор реальный"
