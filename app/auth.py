from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

from .config import Settings


COOKIE_NAME = "kemy_session"
MAX_AGE = 60 * 60 * 24 * 7


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def create_token(username: str, settings: Settings) -> str:
    exp = int(time.time()) + MAX_AGE
    payload = f"{username}:{exp}"
    return f"{_b64(payload.encode())}.{_sign(payload, settings.auth_secret)}"


def verify_token(token: str | None, settings: Settings) -> bool:
    if not token or "." not in token:
        return False
    encoded, signature = token.split(".", 1)
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(padded.encode()).decode()
        username, exp_raw = payload.rsplit(":", 1)
        if username != settings.auth_user or int(exp_raw) < int(time.time()):
            return False
        return hmac.compare_digest(signature, _sign(payload, settings.auth_secret))
    except Exception:
        return False
