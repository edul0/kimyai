from __future__ import annotations

import base64
import hmac
import secrets
import time
from hashlib import sha256

from .config import Settings
from .storage import Storage


COOKIE_NAME = "kemy_session"
MAX_AGE = 60 * 60 * 24 * 7


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def create_token(subject: str, settings: Settings) -> str:
    exp = int(time.time()) + MAX_AGE
    payload = f"{subject}:{exp}"
    return f"{_b64(payload.encode())}.{_sign(payload, settings.auth_secret)}"


def verify_token(token: str | None, settings: Settings) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.split(".", 1)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        _subject, exp_raw = payload.rsplit(":", 1)
        if int(exp_raw) < int(time.time()):
            return False
        return hmac.compare_digest(signature, _sign(payload, settings.auth_secret))
    except Exception:
        return False


def token_subject(token: str | None, settings: Settings) -> str | None:
    if not verify_token(token, settings):
        return None
    encoded = token.split(".", 1)[0]
    padded = encoded + "=" * (-len(encoded) % 4)
    payload = base64.urlsafe_b64decode(padded.encode()).decode()
    subject, _exp_raw = payload.rsplit(":", 1)
    return subject


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}:{digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split(":", 1)
        return hmac.compare_digest(password_hash(password, salt).split(":", 1)[1], digest)
    except Exception:
        return False


def find_user(storage: Storage, email: str) -> dict | None:
    return storage.get_json(f"user:{email.lower()}")


def create_user(storage: Storage, email: str, password: str, name: str = "") -> dict:
    user = {
        "email": email.lower(),
        "name": name or email,
        "password_hash": password_hash(password),
        "created_at": int(time.time()),
    }
    storage.set_json(f"user:{user['email']}", user)
    return user
