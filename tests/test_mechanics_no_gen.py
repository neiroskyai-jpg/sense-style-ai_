"""Механика тарифов и кабинета обязана работать без генерации.

Кредиты у провайдера кончаются, а бизнес-логика от этого зависеть не должна: путь по тарифам,
конструктор капсулы, план недели, переключение сезонов, проверка вещи и ссылка на Карту — всё
это не требует модели. Здесь мы это и доказываем: ни одного обращения к провайдеру.
"""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402

from app import main as m  # noqa: E402
from core import profiles as pr  # noqa: E402
from core import provider  # noqa: E402

USER = "anon-mech"
DIAG = {
    "style_formula": "Классика × Драма",
    "gap_percentage": 38,
    "colortype": "autumn_natural",
    "figure_type": "hourglass",
    "want_traits_top3": ["уверенная", "статусная"],
    "primary_substyle": "Чистая классика",
    "semantic_field_distribution": {"classic": 50, "drama": 30, "romance": 12, "natural": 8},
    "tonal_characteristics": {"contrast": "medium"},
    "visual_formula": {"silhouettes": ["Полуприлегающий силуэт"],
                       "palette": ["Тёплый бежевый", "Графит"],
                       "stop_list": ["Оверсайз без формы"]},
}


@pytest.fixture(autouse=True)
def no_gen(monkeypatch):
    """Выключаем генерацию только на этот модуль: глобальный os.environ протекал
    в соседние тесты и ломал у них сборку Карты."""
    monkeypatch.setenv("SENSE_NO_GEN", "1")


@pytest.fixture
def client(monkeypatch):
    db = Path(tempfile.mkdtemp()) / "profiles.db"
    m.app.config["TESTING"] = True

    def _boom(*a, **k):
        raise AssertionError("механика не должна дёргать модель")

    monkeypatch.setattr(m, "get_profile", lambda e: pr.get_profile(e, db))
    monkeypatch.setattr(m, "save_card", lambda e, c: pr.save_card(e, c, db))
    monkeypatch.setattr(m, "save_diagnosis", lambda e, d: pr.save_diagnosis(e, d, db))
    monkeypatch.setattr(m, "current_card_by_season", lambda e: pr.current_card_by_season(e, db))
    monkeypatch.setattr(m, "card_link_token", lambda e: pr.card_link_token(e, db))
    monkeypatch.setattr(m, "user_by_card_token", lambda t: pr.user_by_card_token(t, db))
    monkeypatch.setattr(m, "gap_progress", lambda e: None)
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "_current_user", lambda: USER)
    monkeypatch.setattr(m, "add_wardrobe_item", lambda e, it: pr.add_wardrobe_item(e, it, db))
    monkeypatch.setattr(m, "wardrobe_items", lambda e: pr.wardrobe_items(e, db))
    monkeypatch.setattr(m, "delete_wardrobe_item", lambda e, i: pr.delete_wardrobe_item(e, i, db))
    monkeypatch.setattr(provider, "chat_json", _boom)      # страховка: модель не зовём
    monkeypatch.setattr(provider, "generate_image", _boom)

    pr.save_diagnosis(USER, DIAG, db)
    pr.save_card(USER, m.build_style_card(DIAG, season="autumn"), db)
    with m.app.test_client() as c:
        yield c, db


def test_card_is_built_without_the_model():
    """Каркас Карты собирается на диагностике и каталоге, без единого вызова провайдера."""
    card = m.build_style_card(DIAG, season="autumn")

    assert card["no_generation"] is True
    assert card["formula"] == "Классика × Драма"
    assert card["gap"] == 38
    assert card["silhouettes"], "силуэты берутся из диагностики"
    assert card["stop_list"], "стоп-лист берётся из диагностики"


def test_card_page_opens_and_admits_it_is_a_skeleton(client):
    c, _ = client

    html = c.get("/card").get_data(as_text=True)

    assert "Это черновик Карты" in html, "клиентка должна понимать, почему нет образов"
    assert "Классика" in html


def test_cabinet_opens_with_working_constructor(client):
    """Капсульный конструктор образов — ядро второго тарифа: вещи и ячейки должны быть на месте."""
    c, _ = client

    html = c.get("/cabinet").get_data(as_text=True)

    assert "Капсульный конструктор образов" in html
    assert "data-cell=" in html, "ячейки образа"
    assert "class=pitem" in html, "вещи для перетаскивания"


def test_week_plan_is_rendered(client):
    """План недели считается сервером и обязан показываться, а не вести на пустой якорь."""
    c, _ = client

    html = c.get("/cabinet").get_data(as_text=True)

    assert "План недели" in html
    assert 'class=weekgrid' in html
    for day in ("Пн", "Ср", "Вс"):
        assert f">{day}<" in html, day


def test_season_switch_rebuilds_capsule(client):
    """Сезонные обновления — это переключатель сезона, он должен работать."""
    c, _ = client

    for season in ("spring", "summer", "winter"):
        r = c.get(f"/cabinet?season={season}")
        assert r.status_code == 200, season


def test_unbuilt_season_does_not_show_stale_generated_looks(client):
    c, db = client
    card = m.build_style_card(DIAG, season="autumn")
    card["looks"] = [{
        "scenario": "деловая встреча",
        "bucket": "Работа",
        "items": ["Тёмный жакет", "Брюки"],
        "img": "AUTUMN-LOOK-SHOULD-NOT-LEAK",
    }]
    pr.save_card(USER, card, db)

    html = c.get("/cabinet?season=spring").get_data(as_text=True)

    assert "AUTUMN-LOOK-SHOULD-NOT-LEAK" not in html
    # Раньше здесь проверялся текст блока «Роли твоей недели» — он убран как дубль плана недели.
    # Смысл проверки тот же: кабинет несобранного сезона живой, а не пустой.
    assert "План недели" in html


def test_capsule_size_toggle_works(client):
    c, _ = client

    assert c.get("/cabinet?items=6").status_code == 200
    assert c.get("/cabinet?items=12").status_code == 200


def test_tariff_routing_follows_user_state(client):
    """Бизнес-логика тарифов: Карта есть — кнопки ведут в Карту и кабинет, а не в квиз."""
    c, _ = client

    assert c.get("/start/card").headers["Location"] == "/card"
    assert c.get("/start/daily").headers["Location"] == "/cabinet"


def test_card_link_works_without_generation(client):
    """Ссылку на Карту можно отдать клиентке и без собранных образов."""
    c, db = client
    token = pr.card_link_token(USER, db)

    r = c.get(f"/card/{token}")

    assert r.status_code == 200
    assert "Классика" in r.get_data(as_text=True)


def test_wardrobe_add_and_remove(client):
    """«Брать / не брать» имеет последствие: вещь попадает в гардероб и убирается из него."""
    c, db = client

    c.post("/wardrobe/add", data={"name": "Жакет структурный", "verdict": "Брать"})
    items = pr.wardrobe_items(USER, db)
    assert len(items) == 1 and items[0]["name"] == "Жакет структурный"

    c.post("/wardrobe/remove", data={"id": items[0]["id"]})
    assert pr.wardrobe_items(USER, db) == []


def test_garment_check_page_opens(client):
    c, _ = client

    assert c.get("/garment").status_code == 200


def test_unbuilt_season_says_so_instead_of_pretending(client):
    """Несобранный сезон не выдаёт каталог за капсулу клиентки.

    Каталог всесезонный, поэтому летний кабинет показывал почти те же вещи, что осенний, — и
    молча называл их капсулой. Теперь подмена подписана, а собранный сезон подписи не несёт.
    """
    c, _ = client

    other = c.get("/cabinet?season=spring").get_data(as_text=True)
    assert "ещё не собрана" in other
    assert "/card?season=spring" in other, "должен быть выход — собрать Карту на этот сезон"

    own = c.get("/cabinet?season=autumn").get_data(as_text=True)
    assert "ещё не собрана" not in own, "на собранном сезоне подпись не нужна"


def test_build_screen_explains_a_lost_job_instead_of_a_dead_end():
    """Задание сборки живёт в памяти сервиса — рестарт его стирает.

    Клиентка видела глухое «Сборка не завершилась» без причины и без понятного шага: статус
    `unknown` обрабатывался вместе с `error`, а сообщения к нему нет вовсе. Теперь у него своя
    ветка, которая называет причину и говорит, что диагностика не потеряна.
    """
    import re

    js = m.CARD_BUILDING

    assert "d.status==='unknown'" in js.replace(" ", "").replace("d.status==='error'||", "") \
        or "unknown" in js
    unknown_branch = js.split("unknown", 1)[1][:600]
    assert "сервис обновился" in unknown_branch
    assert "Собрать заново" in js
    # обещание по времени должно совпадать с реальностью: замер 22.07.2026 — 260 с
    assert "1–2 минуты" not in js, "старая оценка занижена вдвое"
    assert "2–4 минуты" in js


def test_build_screen_offers_a_way_out_in_every_terminal_state():
    """Из любого финального состояния должен быть выход — иначе экран становится тупиком."""
    js = m.CARD_BUILDING

    for state in ("retry", "stale", "unknown", "error"):
        assert f"d.status==='{state}'" in js, state
    assert js.count("fin(") >= 5, "каждое состояние рисуется карточкой с действиями"


def test_cabinet_without_a_card_explains_itself(client):
    """Кабинет без Карты не выкидывает молча на форму сборки.

    Клиентка жала «← в кабинет» из гардероба и оказывалась на форме Карты без единого слова
    почему — читалось как «кнопка не работает».
    """
    c, db = client
    pr.save_card(USER, {}, db)          # диагностика есть, Карты нет

    html = c.get("/cabinet").get_data(as_text=True)

    assert "Сначала — Карта стиля" in html
    assert 'href="/card"' in html
    assert "Пройти диагностику" not in html, "диагностика уже пройдена, не гоняем по кругу"


def test_wardrobe_states_the_photo_rules_before_upload(client):
    """«Не распознано» — почти всегда неподходящий кадр. Правило должно стоять ДО загрузки."""
    c, _ = client

    html = c.get("/wardrobe").get_data(as_text=True)

    assert "Как снимать" in html
    for rule in ("Одна вещь в кадре", "Не на себе", "по одной вещи на кадр"):
        assert rule in html, rule
    assert "HEIC" in html and "Наиболее совместимый" in html, "айфонный формат надо объяснить"


def test_constructor_shows_the_clients_own_capsule_not_only_the_catalog(client):
    """Конструктор собирается из капсулы клиентки, а каталог только добирает пустые слоты.

    Вещь без каталожного фото выбрасывалась из борда. Капсульные вещи описаны языком метода
    («брюки прямого кроя, холодный чёрный») и по тексту в брендовом фиде почти не находятся —
    поэтому теряли фото и вылетали все до одной. Конструктор заполнялся чужим каталогом, и
    вопрос «где моя капсула?» был совершенно справедлив.
    """
    import re

    c, db = client
    card = m.build_style_card(DIAG, season="autumn")
    card["capsule_items"] = ["брюки прямого кроя, холодный чёрный",
                             "жакет структурный, холодный чёрный",
                             "рубашка классического кроя, чистый белый",
                             "балетки, королевский синий"]
    pr.save_card(USER, card, db)

    html = c.get("/cabinet?season=autumn").get_data(as_text=True)
    names = re.findall(r'class=pitem[^>]*data-name="([^"]+)"', html)

    mine = [n for n in names if n in card["capsule_items"]]
    assert len(mine) == len(card["capsule_items"]), f"из капсулы дошло {len(mine)}: {mine}"
    assert "Основа — вещи из твоих образов" in html, "подпись обязана называть источник честно"


def test_capsule_item_without_a_photo_gets_a_designed_tile(client):
    """Плитка без фото — не пустой квадрат: пустой читается как «не загрузилось»."""
    import re

    c, db = client
    card = m.build_style_card(DIAG, season="autumn")
    card["capsule_items"] = ["палантин кашемировый, тёплый серый"]
    pr.save_card(USER, card, db)

    html = c.get("/cabinet?season=autumn").get_data(as_text=True)

    assert re.search(r"<span class=ph0><i>[^<]+</i><b>палантин", html), \
        "у вещи без фото должны быть слот и название на плитке"


def test_assembled_outfit_survives_a_reload(client, monkeypatch):
    """«Мои образы» — последняя невзятая идея из прототипа фаундера.

    Собранный образ жил до перезагрузки: конструктор заставлял собирать заново каждое утро.
    Для тарифа «Стиль каждый день» это и есть главная работа клиентки, поэтому храним на
    сервере, а не в браузере — чистка кэша и смена устройства не должны стирать неделю.
    """
    import json as _json

    c, db = client
    monkeypatch.setattr(m, "saved_outfits", lambda e, **k: pr.saved_outfits(e, db_path=db))
    monkeypatch.setattr(m, "save_outfit", lambda e, t, i: pr.save_outfit(e, t, i, db))
    monkeypatch.setattr(m, "delete_outfit", lambda e, i: pr.delete_outfit(e, i, db))

    assert "Пока пусто. Собери образ слева" in c.get("/cabinet").get_data(as_text=True)

    items = _json.dumps([{"slot": "Верх", "name": "жакет структурный"},
                         {"slot": "Низ", "name": "брюки прямого кроя"}], ensure_ascii=False)
    c.post("/outfits/save", data={"items": items, "title": "Понедельник"})

    html = c.get("/cabinet").get_data(as_text=True)
    assert "Понедельник" in html and "жакет структурный" in html

    saved = pr.saved_outfits(USER, db_path=db)
    c.post("/outfits/remove", data={"id": str(saved[0]["id"])})
    assert pr.saved_outfits(USER, db_path=db) == []


def test_empty_outfit_is_not_saved(client, monkeypatch):
    """Пустой набор — не образ. Кнопка на пустом состоянии заблокирована, сервер тоже не верит."""
    c, db = client
    monkeypatch.setattr(m, "saved_outfits", lambda e, **k: pr.saved_outfits(e, db_path=db))
    monkeypatch.setattr(m, "save_outfit", lambda e, t, i: pr.save_outfit(e, t, i, db))

    c.post("/outfits/save", data={"items": "[]", "title": "Пусто"})
    c.post("/outfits/save", data={"items": "не json", "title": "Мусор"})

    assert pr.saved_outfits(USER, db_path=db) == []
    assert "id=savebtn disabled" in c.get("/cabinet").get_data(as_text=True)


def test_saved_outfits_are_scoped_to_their_owner(client, monkeypatch):
    """Чужой образ чужим не удалить и не увидеть — область по пользователю в самом SQL."""
    import json as _json

    c, db = client
    monkeypatch.setattr(m, "save_outfit", lambda e, t, i: pr.save_outfit(e, t, i, db))
    c.post("/outfits/save", data={"items": _json.dumps([{"slot": "Верх", "name": "Блуза"}]),
                                  "title": "Мой"})
    mine = pr.saved_outfits(USER, db_path=db)
    assert len(mine) == 1

    pr.delete_outfit("someone-else", mine[0]["id"], db)

    assert len(pr.saved_outfits(USER, db_path=db)) == 1, "чужой удалил мой образ"
    assert pr.saved_outfits("someone-else", db_path=db) == []
