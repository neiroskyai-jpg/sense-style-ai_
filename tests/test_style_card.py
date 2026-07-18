"""Тест гарантии «ровно 6 образов» в «Карте стиля» — без API.

LLM капсулы иногда отдаёт меньше образов, чем сценариев. `_ensure_n_looks` должен
всегда вернуть по одному образу на каждый сценарий (переназначение + досборка).
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")  # импорт app.main не должен падать

from app.main import (_card_stale, _daily_cabinet_advice, _daily_week_view, _diag_signature, _ensure_n_looks,
                      _starter_capsule_from_board)  # noqa: E402

SCENARIOS = ["деловая встреча", "свидание", "выходные",
             "презентация", "корпоратив", "путешествие"]
CAPSULE = {"capsule": {"items": [{"name": "жакет"}, {"name": "брюки"}, {"name": "пальто"}]}}
DIAG = {"style_formula": "Классика", "visual_formula": {"silhouettes": ["жакет", "юбка"]}}


def test_pads_missing_scenario_to_six():
    looks = [{"scenario": s, "items": ["x"]} for s in SCENARIOS[:5]]  # нет 'путешествие'
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert [o["scenario"] for o in out] == SCENARIOS
    trip = next(o for o in out if o["scenario"] == "путешествие")
    assert trip["items"]  # дособран из капсулы, не пустой


def test_reassigns_extras_and_synthesizes():
    looks = [{"items": ["x"]}, {"items": ["y"]}]  # без сценариев
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert len({o["scenario"] for o in out}) == 6  # все сценарии уникальны


def test_empty_looks_still_six():
    out = _ensure_n_looks([], SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6
    assert all(o["items"] for o in out)


def test_no_duplicate_scenario_assignment():
    # два образа с одним сценарием — второй уходит в extras и переназначается
    looks = [{"scenario": "работа", "items": ["a"]}, {"scenario": "офис", "items": ["b"]}]
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert len(out) == 6 and len({o["scenario"] for o in out}) == 6


def test_aliases_are_mapped_to_canonical_scenarios():
    looks = [
        {"scenario": "работа", "items": ["жакет"]},
        {"scenario": "повседневное", "items": ["джемпер"]},
        {"scenario": "событие и выход", "items": ["платье"]},
    ]
    out = _ensure_n_looks(looks, SCENARIOS, CAPSULE, DIAG)
    assert [o["scenario"] for o in out][:3] == ["деловая встреча", "свидание", "выходные"]
    assert "корпоратив" in [o["scenario"] for o in out]


def test_starter_capsule_is_nine_items():
    board = [
        {"slot": "Верхний слой", "items": [{"name": "Жакет"}]},
        {"slot": "Верх", "items": [{"name": "Рубашка"}, {"name": "Топ"}, {"name": "Джемпер"}]},
        {"slot": "Низ", "items": [{"name": "Брюки"}, {"name": "Юбка"}]},
        {"slot": "Платья и комбинезоны", "items": [{"name": "Платье"}]},
        {"slot": "Обувь", "items": [{"name": "Лоферы"}]},
        {"slot": "Аксессуары", "items": [{"name": "Сумка"}]},
    ]
    picked, combos = _starter_capsule_from_board(board)
    assert len(picked) == 9
    assert combos >= 18
    assert {item["slot"] for item in picked} >= {"Верх", "Низ", "Обувь"}


def test_daily_cabinet_advice_uses_existing_card_not_new_formula():
    advice = _daily_cabinet_advice(
        {"formula": "Классика × Мягкость", "gap": 62,
         "looks": [{"scenario": "деловая встреча"}]},
        {"style_formula": "Другая формула"},
        {"points": [{"gap": 62}], "delta": 0},
        [{"slot": "Верх", "items": [{"name": "Рубашка"}]},
         {"slot": "Низ", "items": [{"name": "Брюки"}]}],
        [{"name": "Жакет"}],
    )
    assert advice is not None
    assert "Классика × Мягкость" in advice["body"]
    assert "деловая встреча" in " ".join(advice["chips"])


def test_daily_week_view_builds_today_and_week_from_existing_looks():
    view = _daily_week_view(
        {"looks": [
            {"scenario": "деловая встреча", "bucket": "Работа", "items": ["Жакет", "Брюки"], "why_it_works": "Собранно."},
            {"scenario": "свидание", "bucket": "Выход", "items": ["Платье", "Туфли"]},
            {"scenario": "выходные", "bucket": "Повседневное", "items": ["Топ", "Джинсы"]},
        ]},
        [{"slot": "Верх", "items": [{"name": "Рубашка"}]}],
        weekday=5,
    )
    assert view is not None
    assert len(view["week"]) == 7
    assert "Сегодня" in view["today"]["title"]
    assert view["today"]["items"]


# --- инвалидация кэша Карты при новой диагностике (баг рассинхрона Gap квиз↔Карта) ---

_DIAG = {"gap_percentage": 41, "semantic_field_distribution": {"classic": 3, "romance": 2},
         "want_traits_top3": ["элегантная", "умная"], "style_formula": "Классика × Романтика"}


def test_signature_ignores_refined_formula():
    # refine_substyle меняет формулу/подстиль на Карте — отпечаток НЕ должен от этого зависеть
    refined = dict(_DIAG, style_formula="Power Woman", primary_substyle="minimal")
    assert _diag_signature(refined) == _diag_signature(_DIAG)


def test_signature_changes_on_new_gap():
    # заново пройденный квиз с другим Gap → другой отпечаток
    assert _diag_signature(dict(_DIAG, gap_percentage=78)) != _diag_signature(_DIAG)


def test_fresh_card_not_stale():
    card = {"_diag_sig": _diag_signature(_DIAG)}
    refined = dict(_DIAG, style_formula="Power Woman")  # формулу уточнили — не устаревание
    assert _card_stale({"diagnosis": refined, "card": card}) is False


def test_new_quiz_makes_card_stale():
    card = {"_diag_sig": _diag_signature(_DIAG)}
    new_diag = dict(_DIAG, gap_percentage=78, semantic_field_distribution={"drama": 4})
    assert _card_stale({"diagnosis": new_diag, "card": card}) is True


def test_legacy_card_without_signature_stale_on_gap_mismatch():
    # старая Карта без отпечатка: если Gap разошёлся с новой диагностикой — устарела
    # (это был баг: квиз 44%, а Карта показывала прежние 78%)
    assert _card_stale({"diagnosis": dict(_DIAG, gap_percentage=99), "card": {"gap": 41}}) is True


def test_legacy_card_without_signature_same_gap_not_stale():
    # старая Карта без отпечатка, но Gap совпадает → не форсим пересборку
    assert _card_stale({"diagnosis": dict(_DIAG, gap_percentage=41), "card": {"gap": 41}}) is False
