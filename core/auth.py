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

from .config import secret_key

MAGIC_MAX_AGE = 900  # ссылка живёт 15 минут
_SALT = "sense-magic-link"

# транзакционный API Unisender Go (UniOne). Можно переопределить через env, если у тебя
# классический Unisender или другой регион/домен API.
_UNISENDER_URL = os.getenv(
    "UNISENDER_API_URL", "https://go1.unisender.ru/ru/transactional/api/v1/email/send.json"
)


def _serializer() -> URLSafeTimedSerializer:
    # тот же секрет, что и у сессий Flask (env или файл на постоянном томе)
    return URLSafeTimedSerializer(secret_key(), salt=_SALT)


def make_token(email: str) -> str:
    return _serializer().dumps(email.strip().lower())


def read_token(token: str, max_age: int = MAGIC_MAX_AGE) -> str | None:
    """Вернуть email из валидного токена или None (просрочен/подделан)."""
    try:
        return _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def smtp_configured() -> bool:
    """SMTP задан (напр. Яндекс.Почта): нужны логин и пароль приложения."""
    return bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def unisender_configured() -> bool:
    return bool(os.getenv("UNISENDER_API_KEY") and os.getenv("UNISENDER_FROM_EMAIL"))


def email_configured() -> bool:
    """Почта настроена, если задан хотя бы один способ отправки: SMTP или UniSender Go."""
    return smtp_configured() or unisender_configured()


def _subject_and_html(link: str) -> tuple[str, str]:
    return ("Вход в Чувство стиля",
            f"<p>Привет. Чтобы войти, перейди по ссылке (действует 15 минут):</p>"
            f'<p><a href="{link}">Войти в Чувство стиля</a></p>'
            f"<p>Если ты не запрашивала вход — просто проигнорируй письмо.</p>")


def _send_smtp(email: str, link: str) -> bool:
    """Отправка через SMTP (по умолчанию Яндекс: smtp.yandex.ru:465 SSL). From = SMTP_USER —
    Яндекс требует, чтобы отправитель совпадал с авторизованным ящиком. Пароль — «пароль
    приложения» из Яндекс ID (обычный пароль для SMTP не подойдёт)."""
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.utils import formataddr

    host = os.getenv("SMTP_HOST", "smtp.yandex.ru")
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    from_name = os.getenv("SMTP_FROM_NAME", "Чувство стиля")
    subject, html = _subject_and_html(link)
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, user))
    msg["To"] = email
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=20) as s:
                s.login(user, password)
                s.sendmail(user, [email], msg.as_string())
        else:  # 587 — STARTTLS
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(user, password)
                s.sendmail(user, [email], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001 — не роняем вход; вызывающий покажет ссылку админу
        print(f"[SMTP error] {e}")
        return False


def send_magic_link(email: str, link: str) -> bool:
    """Отправить письмо со ссылкой входа. True — отправлено реально; False — dev-фолбэк
    (ничего не настроено): печатаем в лог, вызывающий покажет ссылку на экране.
    Приоритет: SMTP (проще всего — свой ящик) → UniSender Go → dev-фолбэк."""
    if smtp_configured():
        return _send_smtp(email, link)
    if not unisender_configured():
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
