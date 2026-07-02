"""Тест движка шага 4: психотип (Big Five) уточняет подстиль. Без API — провайдер мокается.

Гарантии:
- без психотипа (нет big5) `refine_substyle` возвращает {} и НЕ зовёт LLM (подстиль остаётся из диагностики);
- с психотипом собирает payload (base_style, психотип, hints) и отдаёт результат провайдера.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline  # noqa: E402

DIAG = {
    "base_style": "classic",
    "primary_substyle": "power_woman",
    "semantic_field_distribution": {"classic": 70, "drama": 20, "natural": 10, "romance": 0},
    "want_traits_top3": ["уверенная", "элегантная", "дорогая"],
}


def test_no_psychotype_returns_empty_without_calling_llm(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(pipeline.provider, "chat_json",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    out = pipeline.refine_substyle(DIAG, {"adv": "ноги"})  # deep_intake без big5
    assert out == {}
    assert called["n"] == 0  # LLM не вызывался — экономим токены на базовом уровне


def test_psychotype_drives_substyle(monkeypatch):
    captured = {}

    def fake_chat_json(model, system, user, **kw):
        captured["system"] = system
        captured["user"] = user
        return {"base_style": "classic", "primary_substyle": "minimalism",
                "secondary_substyle": None, "accent_note": None,
                "style_formula": "Минимализм × Soft Classic",
                "substyle_rationale": "Ты по натуре не демонстрируешь — тебе идёт тихая классика."}

    monkeypatch.setattr(pipeline.provider, "chat_json", fake_chat_json)
    deep = {"big5": {"E": "low", "S": "high", "O": "mid", "C": "high", "A": "mid"}}
    out = pipeline.refine_substyle(DIAG, deep)
    assert out["primary_substyle"] == "minimalism"
    assert out["substyle_rationale"]
    # психотип реально ушёл в промпт (payload), а не проигнорирован
    assert "psychotype_levels" in captured["user"]
    assert "25 подстилей" in captured["system"]  # база знаний подклеена
