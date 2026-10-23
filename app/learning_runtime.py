from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .config import Settings
from .storage import Storage


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    provider: str
    specialties: tuple[str, ...]
    context_tokens: int = 0
    local: bool = False
    requires_key: bool = True
    discovered: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["specialties"] = list(self.specialties)
        return data


class ModelCatalog:
    """Catálogo pequeno e extensível; modelos locais descobertos entram em runtime."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._profiles: dict[str, ModelProfile] = {}
        self._health: dict[str, dict[str, Any]] = {}
        self._last_discovery_at = 0.0
        self._register_defaults()

    def _register_defaults(self) -> None:
        defaults = [
            ModelProfile("openai/gpt-oss-120b", "groq", ("coding", "reasoning", "audit"), 131_072),
            ModelProfile(self.settings.gemini_primary_model, "gemini", ("vision", "planning", "documents"), 1_000_000),
            ModelProfile(self.settings.gemini_fallback_model, "gemini", ("vision", "fast", "documents"), 1_000_000),
            ModelProfile("llama3.1-70b", "cerebras", ("audit", "fast"), 8_192),
            ModelProfile(self.settings.ollama_chat_model, "ollama", ("chat", "documents"), local=True, requires_key=False),
            ModelProfile(self.settings.ollama_code_model, "ollama", ("coding", "site"), local=True, requires_key=False),
            ModelProfile(self.settings.ollama_reasoning_model, "ollama", ("reasoning", "audit", "planning"), local=True, requires_key=False),
        ]
        for profile in defaults:
            self._profiles[f"{profile.provider}:{profile.model_id}"] = profile

    def register_discovered(self, provider: str, model_ids: list[str]) -> None:
        for model_id in model_ids:
            key = f"{provider}:{model_id}"
            if key not in self._profiles:
                self._profiles[key] = ModelProfile(
                    model_id=model_id,
                    provider=provider,
                    specialties=("general",),
                    local=provider == "ollama",
                    requires_key=provider != "ollama",
                    discovered=True,
                )

    async def discover_ollama(self, force: bool = False) -> list[str]:
        base_url = str(self.settings.ollama_base_url or "").rstrip("/")
        if not base_url:
            return []
        now = time.time()
        if not force and now - self._last_discovery_at < 60:
            return [p.model_id for p in self._profiles.values() if p.provider == "ollama" and p.discovered]
        self._last_discovery_at = now
        try:
            import httpx

            timeout = httpx.Timeout(4.0, connect=1.5)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(f"{base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
            models = sorted({str(item.get("name") or "").strip() for item in data.get("models", []) if item.get("name")})
            self.register_discovered("ollama", models)
            self._health["ollama"] = {"available": True, "models": models, "checked_at": now}
            return models
        except Exception as exc:
            self._health["ollama"] = {
                "available": False,
                "models": [],
                "checked_at": now,
                "error": type(exc).__name__,
            }
            return []

    def snapshot(self) -> dict[str, Any]:
        configured = self.settings.configured_providers
        profiles = []
        for profile in self._profiles.values():
            item = profile.as_dict()
            item["configured"] = bool(configured.get(profile.provider))
            profiles.append(item)
        return {
            "models": sorted(profiles, key=lambda item: (not item["configured"], item["provider"], item["model_id"])),
            "health": self._health,
        }


class LearningTraceStore:
    """Rastros sem prompt bruto: aprende desempenho sem persistir conteúdo pessoal."""

    def __init__(self, storage: Storage, ttl_seconds: int = 7_776_000, max_traces: int = 500):
        self.storage = storage
        self.ttl_seconds = ttl_seconds
        self.max_traces = max_traces

    def _metric_key(self, mode: str, provider: str) -> str:
        return f"learning:provider:{mode}:{provider}"

    def provider_metrics(self, mode: str, provider: str) -> dict[str, Any]:
        return self.storage.get_json(self._metric_key(mode, provider), {}) or {}

    def quality_metrics(self, mode: str, provider: str) -> dict[str, Any]:
        return self.storage.get_json(f"learning:quality:{mode}:{provider}", {}) or {}

    def record_provider_attempt(
        self,
        mode: str,
        provider: str,
        success: bool,
        latency_ms: float,
        error_type: str = "",
    ) -> None:
        key = self._metric_key(mode, provider)
        item = self.storage.get_json(key, {}) or {}
        attempts = int(item.get("attempts") or 0) + 1
        successes = int(item.get("successes") or 0) + int(success)
        failures = int(item.get("failures") or 0) + int(not success)
        previous_latency = float(item.get("avg_latency_ms") or 0.0)
        average = latency_ms if attempts == 1 else previous_latency + ((latency_ms - previous_latency) / attempts)
        payload = {
            "attempts": attempts,
            "successes": successes,
            "failures": failures,
            "avg_latency_ms": round(average, 3),
            "last_success_at": time.time() if success else float(item.get("last_success_at") or 0.0),
            "last_error": "" if success else error_type[:80],
        }
        self.storage.set_json(key, payload, ttl=self.ttl_seconds)

    def record_delivery(self, mode: str, result: dict[str, Any]) -> None:
        provider = str(result.get("provider") or "unknown")
        evaluation = result.get("evaluation") or {}
        confidence = result.get("confidence") or {}
        trace = {
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "mode": mode,
            "provider": provider,
            "model": str(result.get("model") or ""),
            "quality_score": float(evaluation.get("score") or 0.0),
            "confidence": float(confidence.get("score") or 0.0),
            "validation_ok": bool((result.get("code_validation") or {}).get("ok")),
            "browser_ok": bool((result.get("browser_validation") or {}).get("ok")),
            "tools": sorted(set(str(item) for item in result.get("tools_used") or []))[:20],
            "request_fingerprint": hashlib.sha256(str(result.get("request_contract") or {}).encode()).hexdigest()[:16],
        }
        key = "learning:delivery-traces"
        traces = list(self.storage.get_json(key, []) or [])
        traces.append(trace)
        self.storage.set_json(key, traces[-self.max_traces :], ttl=self.ttl_seconds)
        has_quality_evidence = any(
            isinstance(result.get(name), dict) and bool(result.get(name))
            for name in ("evaluation", "confidence", "code_validation", "browser_validation")
        )
        if not has_quality_evidence:
            return
        quality_key = f"learning:quality:{mode}:{provider}"
        quality = self.storage.get_json(quality_key, {}) or {}
        samples = int(quality.get("samples") or 0) + 1
        previous_score = float(quality.get("avg_quality_score") or 0.0)
        previous_confidence = float(quality.get("avg_confidence") or 0.0)
        quality_score = float(trace["quality_score"])
        confidence_score = float(trace["confidence"])
        payload = {
            "samples": samples,
            "avg_quality_score": round(previous_score + ((quality_score - previous_score) / samples), 3),
            "avg_confidence": round(previous_confidence + ((confidence_score - previous_confidence) / samples), 4),
            "validation_successes": int(quality.get("validation_successes") or 0) + int(trace["validation_ok"]),
            "browser_successes": int(quality.get("browser_successes") or 0) + int(trace["browser_ok"]),
            "updated_at": trace["ts"],
        }
        self.storage.set_json(quality_key, payload, ttl=self.ttl_seconds)

    def snapshot(self) -> dict[str, Any]:
        traces = list(self.storage.get_json("learning:delivery-traces", []) or [])
        return {
            "trace_count": len(traces),
            "recent": traces[-10:],
            "privacy": "prompts e respostas brutas nao sao persistidos nos rastros",
        }
