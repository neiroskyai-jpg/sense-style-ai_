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
    assert "neither older nor younger" in text
    assert "do not beautify" in text  # запрет идеализации никуда не делся


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
