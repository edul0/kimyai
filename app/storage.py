import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

try:
    import redis
except ImportError:  # pragma: no cover - optional cloud dependency
    redis = None


class Storage:
    def __init__(self, redis_url: str | None = None, sqlite_path: str | None = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.sqlite_path = sqlite_path or os.getenv("KEMY_STORAGE_PATH")
        self._memory: dict[str, tuple[Any, float | None]] = {}
        self._redis = None
        self._sqlite: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if self.redis_url and redis:
            try:
                self._redis = redis.from_url(self.redis_url, decode_responses=True)
                self._redis.ping()
            except Exception:
                self._redis = None
        if not self._redis and self.sqlite_path:
            try:
                database_path = Path(self.sqlite_path).expanduser()
                database_path.parent.mkdir(parents=True, exist_ok=True)
                self._sqlite = sqlite3.connect(str(database_path), check_same_thread=False)
                self._sqlite.execute("PRAGMA journal_mode=WAL")
                self._sqlite.execute("PRAGMA synchronous=NORMAL")
                self._sqlite.execute(
                    "CREATE TABLE IF NOT EXISTS kemy_store "
                    "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL)"
                )
                self._sqlite.commit()
            except Exception:
                if self._sqlite:
                    self._sqlite.close()
                self._sqlite = None

    @property
    def backend(self) -> str:
        if self._redis:
            return "redis"
        return "sqlite" if self._sqlite else "memory"

    def status(self) -> dict[str, Any]:
        if self._redis:
            try:
                info = self._redis.info()
                return {
                    "type": "redis",
                    "connected": True,
                    "keys": int(self._redis.dbsize()),
                    "memory": info.get("used_memory_human", ""),
                }
            except Exception as exc:
                return {"type": "redis", "connected": False, "error": str(exc)}
        if self._sqlite:
            self._prune_expired()
            with self._lock:
                count = int(self._sqlite.execute("SELECT COUNT(*) FROM kemy_store").fetchone()[0])
            return {"type": "sqlite", "connected": True, "keys": count, "path": str(self.sqlite_path)}
        self._prune_expired()
        return {"type": "memory", "connected": True, "keys": len(self._memory)}

    def set_json(self, key: str, value: Any, ttl: int | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if self._redis:
            if ttl:
                self._redis.setex(key, ttl, payload)
            else:
                self._redis.set(key, payload)
            return
        expires = time.time() + ttl if ttl else None
        if self._sqlite:
            with self._lock:
                self._sqlite.execute(
                    "INSERT INTO kemy_store(key, payload, expires_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, expires_at=excluded.expires_at",
                    (key, payload, expires),
                )
                self._sqlite.commit()
            return
        self._memory[key] = (value, expires)

    def get_json(self, key: str, default: Any = None) -> Any:
        if self._redis:
            payload = self._redis.get(key)
            return json.loads(payload) if payload else default
        if self._sqlite:
            with self._lock:
                row = self._sqlite.execute(
                    "SELECT payload, expires_at FROM kemy_store WHERE key = ?", (key,)
                ).fetchone()
                if not row:
                    return default
                payload, expires = row
                if expires and time.time() > float(expires):
                    self._sqlite.execute("DELETE FROM kemy_store WHERE key = ?", (key,))
                    self._sqlite.commit()
                    return default
            try:
                return json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                return default
        item = self._memory.get(key)
        if not item:
            return default
        value, expires = item
        if expires and time.time() > expires:
            self._memory.pop(key, None)
            return default
        return value

    def delete(self, key: str) -> None:
        if self._redis:
            self._redis.delete(key)
        elif self._sqlite:
            with self._lock:
                self._sqlite.execute("DELETE FROM kemy_store WHERE key = ?", (key,))
                self._sqlite.commit()
        else:
            self._memory.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        if self._redis:
            return [str(k) for k in self._redis.keys(f"{prefix}*")]
        if self._sqlite:
            self._prune_expired()
            escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            with self._lock:
                rows = self._sqlite.execute(
                    "SELECT key FROM kemy_store WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                    (f"{escaped}%",),
                ).fetchall()
            return [str(row[0]) for row in rows]
        self._prune_expired()
        return [k for k in self._memory if k.startswith(prefix)]

    def _prune_expired(self) -> None:
        now = time.time()
        if self._sqlite:
            with self._lock:
                self._sqlite.execute(
                    "DELETE FROM kemy_store WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
                )
                self._sqlite.commit()
            return
        for key, (_value, expires) in list(self._memory.items()):
            if expires and now > expires:
                self._memory.pop(key, None)

    def close(self) -> None:
        if self._sqlite:
            with self._lock:
                self._sqlite.close()
                self._sqlite = None

