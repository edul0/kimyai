from __future__ import annotations

from typing import Any


class DeliveryEvaluator:
    """Stable, model-independent metrics used to compare pipeline changes over time."""

    def evaluate(self, result: dict[str, Any]) -> dict[str, Any]:
        validation = result.get("code_validation") or {}
        browser = result.get("browser_validation") or {}
        confidence = result.get("confidence") or {}
        files = result.get("files") or []
        metrics = {
            "static_validation": 1.0 if validation.get("ok") else 0.0,
            "browser_validation": 1.0 if browser.get("available") and browser.get("ok") else 0.0,
            "artifact_completeness": 1.0 if files or "<kemy_artifact" in str(result.get("raw") or "").lower() else 0.0,
            "tests_present": 1.0 if self._has_tests(result) else 0.0,
            "evidence_confidence": float(confidence.get("score") or 0.0),
            "autofix_iterations": max(0, len(validation.get("attempts") or []) - 1),
        }
        weighted = (
            metrics["static_validation"] * 0.3
            + metrics["browser_validation"] * 0.2
            + metrics["artifact_completeness"] * 0.2
            + metrics["tests_present"] * 0.15
            + metrics["evidence_confidence"] * 0.15
        )
        return {
            "score": round(weighted * 100, 1),
            "metrics": metrics,
            "grade": "excellent" if weighted >= 0.85 else "good" if weighted >= 0.65 else "needs-improvement",
        }

    @staticmethod
    def _has_tests(result: dict[str, Any]) -> bool:
        raw = str(result.get("raw") or "").lower()
        paths = [
            str(item.get("path") or item.get("name") or "").lower()
            for item in result.get("files") or []
            if isinstance(item, dict)
        ]
        return any(
            path.startswith(("tests/", "test/")) or "test_" in path or ".test." in path or ".spec." in path
            for path in paths
        ) or '<file path="tests/' in raw or ".test." in raw or ".spec." in raw
