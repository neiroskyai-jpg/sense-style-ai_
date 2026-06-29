"""Magic-link аутентификация: самоподписанный токен + отправка письма.

Без паролей и без таблицы токенов: ссылка содержит подписанный email с TTL.
Почта — Unisender (РФ, транзакционный продукт Unisender Go / UniOne). Если ключ/
отправитель не заданы — dev-фолбэк: ссылка возвращается вызывающему (показать на экране),
письмо не отправляется. Это позволяет тестировать вход локально без внешнего сервиса.
"""
from __future__ import annotations
import os

import requests
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

MAGIC_MAX_AGE = 900  # ссылка живёт 15 минут
_SALT = "sense-magic-link"

# транзакционный API Unisender Go (UniOne). Можно переопределить через env, если у тебя
# классический Unisender или другой регион/домен API.
_UNISENDER_URL = os.getenv(
    "UNISENDER_API_URL", "https://go1.unisender.ru/ru/transactional/api/v1/email/send.json"
)


def _serializer() -> URLSafeTimedSerializer:
    # секрет читаем при каждом вызове — чтобы подхватить env без перезапуска при отладке
    secret = os.getenv("SENSE_SECRET_KEY", "dev-insecure-secret-change-in-prod")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def make_token(email: str) -> str:
    return _serializer().dumps(email.strip().lower())


def read_token(token: str, max_age: int = MAGIC_MAX_AGE) -> str | None:
    """Вернуть email из валидного токена или None (просрочен/подделан)."""
    try:
        return _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def email_configured() -> bool:
    return bool(os.getenv("UNISENDER_API_KEY") and os.getenv("UNISENDER_FROM_EMAIL"))


def send_magic_link(email: str, link: str) -> bool:
    """Отправить письмо со ссылкой входа. True — отправлено реально; False — dev-фолбэк
    (ключ не задан): печатаем в лог, вызывающий покажет ссылку на экране."""
    if not email_configured():
        print(f"[DEV magic-link] {email} -> {link}")
        return False
    api_key = os.getenv("UNISENDER_API_KEY")
    from_email = os.getenv("UNISENDER_FROM_EMAIL")
    from_name = os.getenv("UNISENDER_FROM_NAME", "Чувство стиля")
    body = {
        "api_key": api_key,
        "message": {
            "recipients": [{"email": email}],
            "from_email": from_email,
            "from_name": from_name,
            "subject": "Вход в Чувство стиля",
            "body": {
                "html": (
                    f"<p>Привет. Чтобы войти, перейди по ссылке (действует 15 минут):</p>"
                    f'<p><a href="{link}">Войти в Чувство стиля</a></p>'
                    f"<p>Если ты не запрашивала вход — просто проигнорируй письмо.</p>"
                ),
            },
        },
    }
    try:
        r = requests.post(_UNISENDER_URL, json=body, timeout=20)
        if r.status_code >= 400:
            print(f"[Unisender error] {r.status_code}: {r.text[:300]}")
            return False
        return True
    except requests.RequestException as e:  # noqa: BLE001
        print(f"[Unisender exception] {e}")
        return False
