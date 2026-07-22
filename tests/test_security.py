"""Регрессии по итогам секьюрити-ревью 22.07.2026.

Каждый тест здесь once был рабочим эксплойтом на живом коде. Держим их, потому что все четыре
дыры возвращаются одной невинной правкой: снять флаг подтверждения, вернуть константу в фолбэк
секрета, положить фото под именем из формы, поменять |tojson на |safe.
"""
import hashlib
import io
import os

os.environ.setdefault("OPENROUTER_API_KEY", "dummy")

import pytest  # noqa: E402
from flask.sessions import TaggedJSONSerializer  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402
from PIL import Image  # noqa: E402

from app import main as m  # noqa: E402

ADMIN_ROUTES = ("/metrics", "/metrics/leads.csv", "/metrics/unisender.csv",
                "/metrics/feedback.csv", "/metrics/chat.csv")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SENSE_NO_GEN", "1")
    m.app.config["TESTING"] = True
    with m.app.test_client() as c:
        yield c


def test_typing_the_admin_email_does_not_grant_admin(client, monkeypatch):
    """Ввод почты — это не вход.

    При SENSE_OPEN_ACCESS=1 (по умолчанию) почта попадает в сессию без подтверждения. Адрес
    фаундера лежит в коде значением по умолчанию, поэтому любой посетитель вводил его на /login
    и выгружал /metrics/leads.csv: почты, Формулы, цветотипы, разрывы всех клиенток.
    """
    monkeypatch.setenv("SENSE_OPEN_ACCESS", "1")
    monkeypatch.delenv("SENSE_METRICS_KEY", raising=False)

    client.post("/login", data={"email": next(iter(m._ADMIN_EMAILS))})

    for url in ADMIN_ROUTES:
        assert client.get(url).status_code == 302, f"{url} отдался без подтверждённого входа"


def test_verified_flag_does_not_survive_a_change_of_email(client, monkeypatch):
    """Подтвердилась под своей почтой — и сменила её на админскую, унеся флаг. Так нельзя."""
    monkeypatch.setenv("SENSE_OPEN_ACCESS", "1")
    monkeypatch.delenv("SENSE_METRICS_KEY", raising=False)
    with client.session_transaction() as s:
        s["email"] = "client@example.com"
        s["verified"] = True

    client.post("/login", data={"email": next(iter(m._ADMIN_EMAILS))})

    assert client.get("/metrics").status_code == 302


def test_session_cannot_be_forged_with_the_old_hardcoded_secret(client):
    """Фолбэк секрета возвращал константу из публичного репозитория — cookie подделывал кто угодно."""
    old = "dev-insecure-secret-change-in-prod"
    ser = URLSafeTimedSerializer(old, salt="cookie-session", serializer=TaggedJSONSerializer(),
                                 signer_kwargs={"key_derivation": "hmac",
                                                "digest_method": hashlib.sha1})
    client.set_cookie("session", ser.dumps({"email": next(iter(m._ADMIN_EMAILS)),
                                            "verified": True}), domain="localhost")

    assert client.get("/metrics").status_code == 302


def test_secret_key_fallback_is_random_not_constant(monkeypatch):
    """Том недоступен — ключ должен быть случайным. Сессии слетят, но подделать их нельзя."""
    from core import config

    monkeypatch.delenv("SENSE_SECRET_KEY", raising=False)
    monkeypatch.setattr(config, "data_dir", lambda: (_ for _ in ()).throw(OSError("том не примонтирован")))

    first, second = config.secret_key(), config.secret_key()

    assert first != "dev-insecure-secret-change-in-prod"
    assert len(first) > 30 and first != second, "константа вернулась в фолбэк"


def _photo(color):
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(buf, "JPEG")

    class _F:
        filename, mimetype = "photo.jpg", "image/jpeg"

        def read(self):
            return buf.getvalue()

    return _F()


def test_two_clients_with_the_same_filename_do_not_share_a_photo():
    """Камеры телефонов отдают photo.jpg / image.jpg — путь был общим.

    Генерация уходит в фоновый поток и читает файл через минуты. За это время его перезаписывала
    другая женщина, и её лицо попадало в чужую Карту.
    """
    a = m._validate_and_save(_photo((255, 0, 0)))
    b = m._validate_and_save(_photo((0, 0, 255)))
    try:
        assert a != b, "две клиентки пишут в один файл"
        assert Image.open(a).getpixel((5, 5))[0] > 200, "фото клиентки А подменено чужим"
    finally:
        a.unlink(missing_ok=True)
        b.unlink(missing_ok=True)


def test_stored_profile_cannot_break_out_of_the_script_tag():
    """Анкета «Примерочной» не валидируется и уезжала в <script> через json.dumps + |safe."""
    payload = {"impression": "</script><script>alert(1)</script>"}

    with m.app.test_request_context():
        out = m.render_template_string("var s={{ profile|default(none)|tojson }};", profile=payload)

    assert "</script>" not in out
    assert "\\u003c" in out, "разметка обязана быть экранирована"


def test_garment_page_renders_without_a_profile():
    """|tojson по неопределённой переменной роняет страницу — страхуемся default(none)."""
    m.app.config["TESTING"] = True

    assert m.app.test_client().get("/garment").status_code == 200


def test_login_page_never_shows_a_working_link_for_a_typed_admin_email(client, monkeypatch):
    """Ссылку входа нельзя выдавать по введённой почте — её вводит кто угодно.

    Условие «показать ссылку, если введён админский адрес» защиты не давало: посетитель набирал
    адрес фаундера, получал рабочий токен на экране и переходил по нему уже подтверждённым.
    """
    monkeypatch.setenv("SENSE_OPEN_ACCESS", "0")   # ветка с письмом
    monkeypatch.delenv("SENSE_DEV_LINK", raising=False)
    monkeypatch.setattr(m, "send_magic_link", lambda *a, **k: False)   # почта не настроена

    html = client.post("/login", data={"email": next(iter(m._ADMIN_EMAILS))}).get_data(as_text=True)

    assert "/auth?token=" not in html


def test_metrics_key_still_lets_the_founder_in(client, monkeypatch):
    """Закрыв вход по вводу почты, нельзя запереть саму хозяйку: ключ метрик обязан работать."""
    monkeypatch.setenv("SENSE_METRICS_KEY", "s3cret-key")

    assert client.get("/metrics?key=s3cret-key").status_code == 200
    assert client.get("/metrics?key=wrong").status_code == 302
