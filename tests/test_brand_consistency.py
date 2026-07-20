"""Бренд-консистентность: один цвет, один шрифт, один язык.

Аудит перед подачей на конкурс показал разнобой: три оттенка cream, два wine, два ink,
плюс страницы на системной Georgia рядом с продуктом на Cormorant + Onest. По отдельности
мелочь, вместе — ощущение, что сайт собран из кусков.
"""
import io
import re

APP = io.open("app/main.py", encoding="utf-8").read()
INDEX = io.open("web/index.html", encoding="utf-8").read()

# Бренд-токены (visual-direction.md + фактическая палитра продукта)
CREAM, INK, WINE = "#F5EFE3", "#1f1d1b", "#5D2230"


def _values(css_var: str, text: str) -> set[str]:
    return set(re.findall(rf"--{css_var}:(#[0-9A-Fa-f]{{6}})", text))


def test_one_cream_one_ink_one_wine():
    """Почти одинаковые оттенки одного токена — самый заметный признак небрежной сборки."""
    assert _values("cream", APP) <= {CREAM}, _values("cream", APP)
    assert _values("ink", APP) <= {INK}, _values("ink", APP)
    # wine2 — осознанный второй тон градиента, у него своё имя; сам wine должен быть один
    assert _values("wine", APP) <= {WINE}, _values("wine", APP)


def test_no_system_serif_as_body_font():
    """Georgia в body — системный сериф вместо бренд-шрифта: страница читается как чужая."""
    assert "font-family:Georgia,serif;max-width" not in APP
    assert "margin:0;font-family:Georgia,serif" not in APP


def test_tone_of_voice_has_no_banned_words():
    """Восемь правил ToV из CLAUDE.md. Промпты не проверяем — там эти слова стоят как запреты."""
    banned = ("потрясающ", "великолепн", "изумительн", "обалденн",
              "вы молодец", "просто супер", "настоящая магия")
    for word in banned:
        assert word not in APP.lower(), word
        assert word not in INDEX.lower(), word


def test_client_is_addressed_informally():
    """Обращение — только на «ты». «Вы» в интерфейсе ломает тон всего продукта."""
    for text, name in ((APP, "app/main.py"), (INDEX, "web/index.html")):
        hits = re.findall(r"(?:^|[\s>«\"'(])([Вв]ы|[Вв]ам|[Вв]ас|[Вв]аш\w*)\s+[а-яё]{3,}", text)
        assert not hits, f"{name}: {hits[:5]}"
