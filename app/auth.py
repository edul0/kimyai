from __future__ import annotations

import base64
import hmac
import secrets
import time
from hashlib import pbkdf2_hmac, sha256

from .config import Settings
from .storage import Storage


COOKIE_NAME = "kemy_session"
MAX_AGE = 60 * 60 * 24 * 7
PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000


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


def password_hash(password: str, salt: str | None = None, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
    return f"{PASSWORD_ALGORITHM}${iterations}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith(f"{PASSWORD_ALGORITHM}$"):
            _algorithm, iterations_raw, salt, digest = stored.split("$", 3)
            candidate = password_hash(password, salt, int(iterations_raw)).rsplit("$", 1)[-1]
            return hmac.compare_digest(candidate, digest)
        # Compatibilidade com contas antigas; o login bem-sucedido migra o hash.
        salt, digest = stored.split(":", 1)
        legacy = sha256(f"{salt}:{password}".encode()).hexdigest()
        return hmac.compare_digest(legacy, digest)
    except Exception:
        return False


def password_needs_rehash(stored: str) -> bool:
    try:
        algorithm, iterations_raw, _salt, _digest = stored.split("$", 3)
        return algorithm != PASSWORD_ALGORITHM or int(iterations_raw) < PASSWORD_ITERATIONS
    except (TypeError, ValueError):
        return True


def upgrade_user_password(storage: Storage, user: dict, password: str) -> dict:
    upgraded = dict(user)
    upgraded["password_hash"] = password_hash(password)
    upgraded["password_updated_at"] = int(time.time())
    storage.set_json(f"user:{str(upgraded['email']).lower()}", upgraded)
    return upgraded


class AuthAttemptLimiter:
    """Contador compartilhável via Storage, sem guardar email ou IP em texto puro."""

    def __init__(self, storage: Storage, limit: int = 5, window_seconds: int = 300):
        self.storage = storage
        self.limit = max(1, limit)
        self.window_seconds = max(1, window_seconds)

    def _key(self, scope: str, identifier: str) -> str:
        fingerprint = sha256(str(identifier or "anonymous").strip().lower().encode()).hexdigest()[:32]
        return f"auth_attempt:{scope}:{fingerprint}"

    def retry_after(self, scope: str, identifier: str) -> int:
        entry = self.storage.get_json(self._key(scope, identifier), {}) or {}
        count = int(entry.get("count") or 0)
        expires_at = float(entry.get("expires_at") or 0)
        if count < self.limit or expires_at <= time.time():
            return 0
        return max(1, int(expires_at - time.time()))

    def fail(self, scope: str, identifier: str) -> int:
        key = self._key(scope, identifier)
        now = time.time()
        entry = self.storage.get_json(key, {}) or {}
        expires_at = float(entry.get("expires_at") or 0)
        count = int(entry.get("count") or 0) if expires_at > now else 0
        count += 1
        expires_at = now + self.window_seconds
        self.storage.set_json(
            key,
            {"count": count, "expires_at": expires_at},
            ttl=self.window_seconds,
        )
        return max(0, self.limit - count)

    def clear(self, scope: str, identifier: str) -> None:
        self.storage.delete(self._key(scope, identifier))


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
