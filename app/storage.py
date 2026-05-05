import json
import os
import time
from typing import Any

try:
    import redis
except ImportError:  # pragma: no cover - optional cloud dependency
    redis = None


class Storage:
    def __init__(self, redis_url: str | None = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self._memory: dict[str, tuple[Any, float | None]] = {}
        self._redis = None
        if self.redis_url and redis:
            try:
                self._redis = redis.from_url(self.redis_url, decode_responses=True)
                self._redis.ping()
            except Exception:
                self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self._redis else "memory"

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
        self._memory[key] = (value, expires)

    def get_json(self, key: str, default: Any = None) -> Any:
        if self._redis:
            payload = self._redis.get(key)
            return json.loads(payload) if payload else default
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
        else:
            self._memory.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        if self._redis:
            return [str(k) for k in self._redis.keys(f"{prefix}*")]
        self._prune_expired()
        return [k for k in self._memory if k.startswith(prefix)]

    def _prune_expired(self) -> None:
        now = time.time()
        for key, (_value, expires) in list(self._memory.items()):
            if expires and now > expires:
                self._memory.pop(key, None)

