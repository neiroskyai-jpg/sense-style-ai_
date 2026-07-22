"""Канон метода: 4 стиля и 25 подстилей, ничего сверх этого.

Фаундер, 19.07.2026: «сделай правило, чтобы ничего не было придумано, а взято из методологии
и базы персонального стиля». До этого клиентка получала ярлыки, которых в методе нет:
«Нежная Реформаторская», «Структурный Романтизм», «Soft Classic», «Драма-акцент».
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import canon  # noqa: E402
from core import pipeline as pl  # noqa: E402


def test_method_gives_exactly_25_substyles():
    """Список тянется из sense-style-method.md — единственного источника правды."""
    assert len(canon.substyles()) == 25


def test_known_canonical_names_are_present():
    names = canon.substyles()

    for expected in ("Чистая классика", "Феминная драма", "Леди-лайк", "Smart Casual",
                     "Quiet Luxury", "Power Woman", "Бохо и бохо-шик"):
        assert expected in names, expected


def test_invented_labels_snap_back_to_the_method():
    """Ярлыки, которые модель реально выдавала, приводятся к каноническим именам."""
    assert canon.snap_substyle("Soft Classic") == "Чистая классика"
    assert canon.snap_substyle("Драма-акцент") == "Феминная драма"
    assert canon.snap_substyle("Структурный Романтизм") == "Чистый романтизм"


def test_canonical_names_pass_through_unchanged():
    for name in ("Леди-лайк", "Smart Casual", "Quiet Luxury"):
        assert canon.snap_substyle(name) == name


def test_unrecognisable_label_is_not_guessed():
    """Лучше ничего, чем случайный подстиль: подмена — это чужая диагностика."""
    assert canon.snap_substyle("нечто неопознанное") == ""


def test_diagnosis_substyles_are_enforced():
    diag = {"primary_substyle": "Soft Classic", "secondary_substyle": "Драма-акцент",
            "base_style": "classic"}

    canon.enforce_substyles(diag)

    assert diag["primary_substyle"] == "Чистая классика"
    assert diag["secondary_substyle"] == "Феминная драма"


def test_rule_lists_the_canon_and_forbids_invention():
    rule = canon.canon_rule()

    assert "25 подстилей" in rule and "Леди-лайк" in rule
    assert "НЕ СУЩЕСТВУЕТ" in rule
    assert "Soft Classic" in rule, "запрет должен называть реально встречавшиеся выдумки"


def test_every_generative_step_carries_the_rule(monkeypatch):
    """Правило должно доезжать до всех шагов, где рождаются названия."""
    seen = []
    monkeypatch.setattr(pl.provider, "chat_json",
                        lambda model, system, user, **kw: seen.append(system) or {})
    diag = {"style_formula": "Классика", "figure_type": "hourglass",
            "visual_formula": {"silhouettes": ["Прямой"], "palette": ["беж"]}}

    pl.diagnose({}, {}, mode="dev")
    pl.generate_directions(diag, {}, mode="dev")
    pl.generate_capsule(diag, {"mode": "capsule", "n_looks": 1}, mode="dev")

    assert len(seen) == 3
    for system in seen:
        assert "КАНОН МЕТОДА" in system


def test_english_substyles_are_shown_in_russian():
    """Три подстиля метода названы по-английски — клиентке их показываем по-русски.

    В данных остаётся канон: по нему работают enforce_substyles и промпты. Но «Мягкое прочтение
    Power Woman» на экране читается как чужое, а лендинг давно говорит «Сильная женщина».
    """
    from core.canon import ru_display, substyles

    assert ru_display("Чистая классика × Power Woman") == "Чистая классика × Сильная женщина"
    assert ru_display("Quiet Luxury") == "Тихая роскошь"
    assert ru_display("Smart Casual") == "Смарт-кэжуал"
    assert ru_display(None) == ""
    # канон не тронут: в списке подстилей имена остаются английскими
    assert "Power Woman" in substyles()


def test_canon_rule_tells_the_model_to_write_those_names_in_russian():
    """Подстановка задним числом ломает падеж («прочтение Сильная женщина»), поэтому русское
    имя должна писать сама модель — фразу она строит целиком."""
    from core.canon import canon_rule

    rule = canon_rule()

    assert "Сильная женщина" in rule and "Сильной женщины" in rule
    assert "primary_substyle" in rule, "служебные поля обязаны остаться каноническими"
