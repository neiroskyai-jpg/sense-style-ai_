"""Кроп головы даёт модели КРУПНОЕ лицо — иначе она додумывает чужое.

Реальная жалоба клиенток (17.07.2026): «лицо не моё / нечёткое». Причина была в референсе:
head_crop брал верхнюю полосу во всю ширину и апскейлил её по длинной стороне — лицо оставалось
~90px посреди неба и фасада, а «крупный кадр головы» был фикцией. Теперь лицо ищет детектор
(YuNet), кадр строится вокруг него.
"""
import base64
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402
from PIL import Image  # noqa: E402

from core.provider import _detect_face, head_crop  # noqa: E402

PHOTO = "web/photos/hero/01-do.jpg"      # реальное фото: женщина у здания (барельефы в кадре)


def _decode(data_url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


@pytest.mark.skipif(not os.path.exists(PHOTO), reason="нет фото в этом окружении")
def test_lico_naydeno_i_odno():
    """Детектор находит лицо клиентки, а не барельефы на фасаде (Haar находил 8 «лиц»)."""
    face = _detect_face(Image.open(PHOTO).convert("RGB"))
    if face is None:
        pytest.skip("opencv/модель недоступны — кроп работает по фолбэку")
    x, y, w, h = face
    img = Image.open(PHOTO)
    assert 0 <= x < img.width and 0 <= y < img.height
    assert w > 40 and h > 40, "рамка лица подозрительно мала"


@pytest.mark.skipif(not os.path.exists(PHOTO), reason="нет фото в этом окружении")
def test_lico_v_krope_krupnoe():
    """Главное: в кропе лицо занимает заметную долю кадра, а не теряется в фоне."""
    face = _detect_face(Image.open(PHOTO).convert("RGB"))
    if face is None:
        pytest.skip("opencv/модель недоступны")
    crop = _decode(head_crop(PHOTO))
    # кадр строится как ~3.2 ширины лица → доля лица ≈ 30%; было ~9% от полосы во всю ширину
    share = face[2] * max(1.0, 1024 / max(face[2] * 3.2, face[3] * 3.6)) / crop.width
    assert share > 0.2, f"лицо занимает лишь {share:.0%} кропа — модель будет додумывать"
    assert crop.height >= crop.width, "портретный кадр головы, а не панорама"


@pytest.mark.skipif(not os.path.exists(PHOTO), reason="нет фото в этом окружении")
def test_krop_ne_padaet_i_vozvrashaet_data_url():
    out = head_crop(PHOTO)
    assert out and out.startswith("data:image/jpeg;base64,")
    assert len(out) > 10_000


def test_bitoe_foto_ne_ronyaet_render(tmp_path):
    """Кроп — вспомогательный шаг: битый файл не должен валить генерацию образа."""
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"not an image")
    assert head_crop(str(bad)) is None
