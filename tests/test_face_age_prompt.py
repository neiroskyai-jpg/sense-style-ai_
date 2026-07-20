"""Рендер не должен СТАРИТЬ клиентку.

Жалоба фаундера: на сгенерированных образах женщина выглядит старше себя. Причина была в самой
инструкции: мы одновременно просили «скопируй её лицо и возраст точно» и требовали фото-финиш
с «visible texture, pores and fine lines» на плёнке Portra с documentary-подачей. Модель честно
ДОРИСОВЫВАЛА морщины, которых на референсе нет. Текстуру кожи воспроизводим по референсу.

Тест держит это требование: возврат «fine lines» в общий финиш ломает сборку.
"""
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

from core import pipeline as p  # noqa: E402


def test_photo_finish_does_not_request_wrinkles():
    """Финиш не заказывает морщины — иначе модель добавит их поверх реального лица."""
    finish = p._PHOTO_FINISH.lower()
    assert "fine lines" not in finish
    assert "wrinkle" not in finish
    # текстура кожи по-прежнему нужна: без неё лицо уходит в пластик
    assert "texture" in finish and "no skin smoothing" in finish
    assert "reference photo" in finish  # текстуру берём с фото, а не выдумываем


def test_instruction_forbids_ageing(monkeypatch):
    """В инструкции рендера есть явный запрет старить — и он симметричен запрету «улучшать»."""
    captured = {}

    def fake_generate_image(instruction, model=None, ref_images=None):
        captured["instruction"] = instruction
        return ["data:image/png;base64,AA=="]

    monkeypatch.setattr(p.provider, "generate_image", fake_generate_image)
    monkeypatch.setattr(p.provider, "encode_image", lambda *a, **k: "data:image/png;base64,BB==")
    monkeypatch.setattr(p.provider, "head_crop", lambda *a, **k: "data:image/png;base64,CC==")

    p.render_look_on_client("photo.jpg", "wool coat, city street")

    text = captured["instruction"].lower()
    assert "do not add wrinkles" in text
    assert "never older" in text
    assert "do not beautify" in text  # черты лица не трогаем
    # Запрос фаундера: образ должен молодить, а не старить. Симметрию «ни старше, ни моложе»
    # сняли сознательно — клиентку показываем отдохнувшей и в лучшей форме. Но граница жёсткая:
    # свет и подача, а не другое лицо и не подмена фигуры.
    assert "well-rested version of herself at her real age" in text
    assert "making her thinner, or swapping in a younger face is not" in text


def test_face_and_body_references_both_passed(monkeypatch):
    """Личность держится на двух референсах: крупное лицо + фигура. Порядок важен — лицо первым."""
    seen = {}

    def fake_generate_image(instruction, model=None, ref_images=None):
        seen["refs"] = ref_images
        return ["data:image/png;base64,AA=="]

    monkeypatch.setattr(p.provider, "generate_image", fake_generate_image)
    monkeypatch.setattr(p.provider, "encode_image", lambda *a, **k: "BODY")
    monkeypatch.setattr(p.provider, "head_crop", lambda *a, **k: "FACE")

    p.render_look_on_client("photo.jpg", "wool coat")
    assert seen["refs"] == ["FACE", "BODY"]


def test_trend_canon_reaches_the_render(monkeypatch):
    """Тренды сезона обязаны доезжать до картинки, а не лежать документом в architecture/."""
    captured = {}

    def fake_generate_image(instruction, model=None, ref_images=None):
        captured["instruction"] = instruction
        return ["data:image/png;base64,AA=="]

    monkeypatch.setattr(p.provider, "generate_image", fake_generate_image)
    monkeypatch.setattr(p.provider, "encode_image", lambda *a, **k: "BODY")
    monkeypatch.setattr(p.provider, "head_crop", lambda *a, **k: "FACE")

    p.render_look_on_client("photo.jpg", "wool coat")

    text = captured["instruction"].lower()
    assert "current 2026-2027 season notes" in text
    assert "one deliberate trend accent per look" in text


def test_provocative_trends_are_blocked_for_daytime():
    """Прозрачность, корсеты и голая талия — подиумная провокация. В переговорной она работает
    против клиентки 30-50, ради которой строится продукт. Допустимы только вечер и свидание."""
    canon = p._TREND_CANON.lower()
    assert "never use sheer fabrics" in canon
    assert "corsets or bustiers" in canon
    assert "bare midriffs" in canon
    assert "her formula wins" in canon, "психология прежде моды — тренд не перебивает Формулу"


def test_trend_canon_carries_the_course_rules():
    """Канон должен нести конкретику курса, а не общие слова про моду.

    Без имён модель воспроизводит ровно те клише, которые методология считает устаревшими.
    """
    canon = p._TREND_CANON.lower()
    # пропорция гардероба: база несёт, тренд украшает
    assert "70-80% long-lasting base" in canon
    # правило длин из курса
    assert "the wider the trousers the longer" in canon
    # главный оттенок сезона
    assert "hot chocolate" in canon
    # стоп-лист: самое узнаваемое устаревшее
    for dated in ("skinny jeans", "3/4 or rolled-up sleeves", "teddy-bear coats",
                  "long ugg boots", "micro bags", "mixing warm and cold beige"):
        assert dated in canon, dated
