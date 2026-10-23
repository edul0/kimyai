import time
import unittest
from hashlib import sha256

from app.auth import (
    AuthAttemptLimiter,
    PASSWORD_ITERATIONS,
    create_user,
    find_user,
    password_hash,
    password_needs_rehash,
    upgrade_user_password,
    verify_password,
)
from app.storage import Storage


class AuthSecurityTests(unittest.TestCase):
    def test_password_uses_slow_versioned_hash(self):
        stored = password_hash("senha-segura")
        self.assertTrue(stored.startswith(f"pbkdf2_sha256${PASSWORD_ITERATIONS}$"))
        self.assertTrue(verify_password("senha-segura", stored))
        self.assertFalse(verify_password("senha-errada", stored))
        self.assertFalse(password_needs_rehash(stored))

    def test_legacy_hash_is_accepted_and_upgraded(self):
        storage = Storage()
        salt = "legacy-salt"
        legacy = f"{salt}:{sha256(f'{salt}:senha-antiga'.encode()).hexdigest()}"
        storage.set_json("user:pessoa@example.com", {
            "email": "pessoa@example.com",
            "password_hash": legacy,
        })
        user = find_user(storage, "pessoa@example.com")
        self.assertTrue(verify_password("senha-antiga", user["password_hash"]))
        self.assertTrue(password_needs_rehash(user["password_hash"]))
        upgrade_user_password(storage, user, "senha-antiga")
        upgraded = find_user(storage, "pessoa@example.com")
        self.assertTrue(verify_password("senha-antiga", upgraded["password_hash"]))
        self.assertFalse(password_needs_rehash(upgraded["password_hash"]))

    def test_attempt_limiter_blocks_and_can_be_cleared(self):
        storage = Storage()
        limiter = AuthAttemptLimiter(storage, limit=2, window_seconds=30)
        self.assertEqual(limiter.retry_after("login", "user@example.com"), 0)
        limiter.fail("login", "user@example.com")
        limiter.fail("login", "user@example.com")
        self.assertGreater(limiter.retry_after("login", "user@example.com"), 0)
        limiter.clear("login", "user@example.com")
        self.assertEqual(limiter.retry_after("login", "user@example.com"), 0)

    def test_attempt_limiter_does_not_store_identifier(self):
        storage = Storage()
        limiter = AuthAttemptLimiter(storage, limit=1, window_seconds=30)
        limiter.fail("login", "secret@example.com")
        self.assertFalse(any("secret@example.com" in key for key in storage.keys("auth_attempt:")))


if __name__ == "__main__":
    unittest.main()
