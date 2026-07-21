"""Разбор личного гардероба: что оставить, сколько образов уже есть, чего не хватает.

Главный аргумент продукта — «из ТВОИХ вещей уже собирается N образов», а не «купи ещё».
Поэтому вся арифметика тут детерминированная: vision распознаёт вещь по фото, а решения
о капсуле и числа считает код.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from app import main as m  # noqa: E402

ITEMS = [
    {"name": "Блузка шёлковая кремовая", "verdict": "take"},
    {"name": "Топ из вискозы", "verdict": "take"},
    {"name": "Брюки палаццо мокко", "verdict": "take"},
    {"name": "Джинсы скинни", "verdict": "skip"},
    {"name": "Жакет твидовый", "verdict": "replace"},
]


def test_breakdown_splits_by_verdict():
    parts = m.wardrobe_breakdown(ITEMS)

    assert [i["name"] for i in parts["drop"]] == ["Джинсы скинни"]
    assert [i["name"] for i in parts["fix"]] == ["Жакет твидовый"]
    assert len(parts["keep"]) == 3


def test_unchecked_item_is_not_taken_away():
    """Вещь без вердикта добавлена руками. Не выбрасываем её молча, пока не проверили."""
    parts = m.wardrobe_breakdown([{"name": "Пальто бабушкино"}])

    assert len(parts["keep"]) == 1
    assert parts["drop"] == []


def test_summary_counts_looks_without_any_purchase():
    """Число, ради которого всё это: сколько комплектов собирается из своих вещей."""
    s = m.wardrobe_summary(ITEMS)

    # 2 верха (блузка, топ) + 1 жакет «с оговоркой» носится, 1 низ → 2 комплекта
    assert s["looks_now"] == 2
    assert s["keep_count"] == 3 and s["fix_count"] == 1 and s["drop_count"] == 1


def test_gaps_follow_method_quotas():
    """Пробел — это отклонение от квот метода, а не «мало вещей» на глаз."""
    gaps = {g["slot"]: g for g in m.wardrobe_summary(ITEMS)["gaps"]}

    assert "Обувь" in gaps and gaps["Обувь"]["have"] == 0
    assert "Низ" in gaps and gaps["Низ"]["have"] == 1


def test_suggestions_are_capped_and_explained():
    """Максимум две докупки, каждая — под конкретный пробел и с вкладом в образы."""
    sug = m.wardrobe_suggestions(ITEMS)

    assert len(sug) <= 2
    for s in sug:
        assert s["why"], s
        assert s["adds_looks"] >= 1, "предлагать вещь, которая ничего не добавляет, незачем"


def test_empty_slot_and_thin_slot_are_worded_differently():
    """Сказать «нет вещи» там, где вещь есть, — прямая неправда: клиентка видит свой шкаф."""
    thin = m.wardrobe_suggestions(ITEMS)
    assert any("пока" in s["why"] for s in thin)

    empty = m.wardrobe_suggestions([{"name": "Блузка", "verdict": "take"}])
    assert any("нет ни одной вещи" in s["why"] for s in empty)


def test_no_gaps_means_no_suggestions():
    """Нет пробелов — нет предложений. Покупок ради покупок не бывает."""
    full = [{"name": n, "verdict": "take"} for n in
            ("Блузка", "Рубашка", "Топ шёлковый", "Брюки прямые", "Юбка миди",
             "Жакет", "Ботильоны", "Сумка-тоут")]

    assert m.wardrobe_suggestions(full) == []


def _client(monkeypatch, items, diag=True):
    m.app.config["TESTING"] = True
    store = {"diagnosis": {"style_formula": "Классика"} if diag else {}}
    monkeypatch.setattr(m, "get_profile", lambda e: store)
    monkeypatch.setattr(m, "wardrobe_items", lambda e: items)
    monkeypatch.setattr(m, "_current_user", lambda: "wd-test")
    return m.app.test_client()


def test_page_opens_and_shows_the_headline_number(monkeypatch):
    """Число «образов из твоих вещей» — то, ради чего страница существует."""
    html = _client(monkeypatch, ITEMS).get("/wardrobe").get_data(as_text=True)

    assert "Мой гардероб" in html
    assert "из твоих вещей" in html
    assert "Без единой покупки" in html


def test_page_requires_diagnosis_first(monkeypatch):
    """Без Формулы вердикт не с чем сверять — ведём на диагностику, а не показываем пустоту."""
    html = _client(monkeypatch, [], diag=False).get("/wardrobe").get_data(as_text=True)

    assert "после диагностики" in html


def test_upload_is_capped(monkeypatch):
    """Каждая вещь — vision-вызов: без предела один клик съедал бы квоту ключа."""
    assert m.MAX_WARDROBE_UPLOAD <= 12


def test_cabinet_links_to_wardrobe():
    """Страница без входа бесполезна: из кабинета до неё должен быть путь."""
    assert '/wardrobe"' in m.CABINET_PAGE


def test_upload_survives_broken_file_and_db_failure(monkeypatch):
    """Одна проблемная вещь не должна стоить всей загрузки.

    На проде /wardrobe/upload отдавал Internal Server Error: под защитой стояло только
    распознавание, а валидация файла (кидает и OSError), запись в базу и счётчик вызовов —
    снаружи. Любая из них роняла запрос, и клиентка вместо гардероба видела 500.
    """
    import io as _io

    m.app.config["TESTING"] = True
    monkeypatch.setattr(m, "get_profile", lambda e: {"diagnosis": {"style_formula": "Power Woman"}})
    monkeypatch.setattr(m, "_current_user", lambda: "u")
    monkeypatch.setattr(m, "record_event", lambda *a, **k: None)

    def boom(*a, **k):
        raise OSError("диск недоступен")

    monkeypatch.setattr(m, "add_wardrobe_item", boom)

    r = m.app.test_client().post(
        "/wardrobe/upload",
        data={"photos": (_io.BytesIO(b"not-an-image"), "x.jpg")},
        content_type="multipart/form-data")

    assert r.status_code == 302, "загрузка обязана вернуть страницу, а не 500"
