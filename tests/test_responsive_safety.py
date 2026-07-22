"""Страницы не должны разъезжаться по горизонтали ни на телефоне, ни на десктопе.

Найдено прогоном на 390/768/1440: на «Брать или не брать» страница уезжала в горизонтальный
скролл (592px при экране 390). Виноват был скрытый чекбокс внутри чипа — он наследовал общее
правило input{width:100%} и растягивался на всю форму, оставаясь невидимым.

Тест держит структурные предпосылки, а не пиксели: проверять реальную вёрстку — работа
браузерного прогона, но эти два правила обязаны быть в каждом шаблоне.
"""
import io
import re

APP = io.open("app/main.py", encoding="utf-8").read()

TEMPLATES = ["FORM", "RESULT", "GARMENT_FORM", "GARMENT_RESULT", "LOGIN_PAGE", "ME_PAGE",
             "CARD_BUILDING", "STYLIST_PAGE", "PRIVACY", "NEED_DIAGNOSIS", "CARD_BUILD_FORM",
             "STYLE_CARD", "CABINET_PAGE", "STYLEBOOK_PAGE"]


def _body(name: str) -> str:
    m = re.search(rf'\n{name} = """', APP)
    assert m, f"шаблон {name} не найден"
    return APP[m.end():APP.find('"""', m.end())]


def test_every_page_declares_viewport():
    """Без метатега мобильный браузер рисует страницу как десктопную и всё уезжает."""
    for name in TEMPLATES:
        assert "width=device-width" in _body(name), name


def test_every_page_sets_border_box():
    """С content-box padding прибавляется к width:100% — классическая причина переполнения."""
    for name in TEMPLATES:
        assert "box-sizing:border-box" in _body(name), name


def test_hidden_chip_inputs_have_no_size():
    """Невидимый чекбокс не должен занимать место: он ловил input{width:100%} и ломал вёрстку."""
    for rule in re.findall(r"\.(?:chip|scalechip) input\{[^}]*\}", APP):
        assert "width:0" in rule, rule
        assert "height:0" in rule, rule


def test_no_rigid_min_width_blocks_the_phone():
    """min-width больше телефонного экрана не даёт блоку сжаться и уводит страницу в скролл.

    Проверяем именно её: overflow-x на контейнерах тут не нужен — сетки на grid с minmax
    переносятся сами, и требовать от них скролл значило бы проверять решение, а не проблему.
    """
    rigid = []
    for name in TEMPLATES:
        # Условия медиазапросов (@media(min-width:1480px)) — это не размер блока, а порог,
        # на котором раскладка меняется. Их вырезаем, иначе тест ловит сам себя.
        css = re.sub(r"@media[^{]*", "", _body(name))
        for value in re.findall(r"min-width:\s*(\d+)px", css):
            if int(value) > 380:
                rigid.append((name, value))
    assert not rigid, rigid


def test_css_never_puts_a_brace_next_to_a_hash():
    """`{#` внутри CSS Jinja читает как начало СВОЕГО комментария и съедает остаток шаблона.

    Реальный случай 23.07.2026: правило `@media(max-width:900px){#palette .palcols{...}}`
    проглотило всё до конца файла вместе с закрывающим тегом style. Страница отдалась с кодом
    200 и осталась пустой: тело документа уехало внутрь стиля. Ошибка бесшумная — ни исключения,
    ни предупреждения, и заметна только глазами в браузере.

    Проверяем именно CSS-блоки: в разметке `{#` — это законный комментарий Jinja.
    """
    bad = []
    pos = 0
    while True:
        a = APP.find("<style>", pos)
        if a < 0:
            break
        b = APP.find("</style>", a)
        css = APP[a:b if b > a else a + 40000]
        if "{#" in css:
            line = APP[:a + css.index("{#")].count(chr(10)) + 1
            bad.append(f"строка {line}")
        pos = (b if b > a else a) + 1

    assert not bad, ("скобка вплотную к решётке в CSS — шаблон обрежется: " + "; ".join(bad))


def test_every_template_closes_its_style_block():
    """Незакрытый style утягивает в себя всю страницу — тело документа остаётся пустым."""
    src = APP

    assert src.count("<style>") == src.count("</style>"), \
        "число открывающих и закрывающих тегов style разошлось"
