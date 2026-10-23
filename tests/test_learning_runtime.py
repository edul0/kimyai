import unittest

from app.config import Settings
from app.learning_runtime import LearningTraceStore, ModelCatalog
from app.llm_router import LLMRouter
from app.storage import Storage


class LearningRuntimeTests(unittest.TestCase):
    def test_router_reloads_learned_provider_metrics(self):
        storage = Storage()
        settings = Settings(
            LLM_MODE="providers",
            GROQ_API_KEY="g",
            GEMINI_API_KEY="m",
        )
        first = LLMRouter(settings, storage=storage)
        for _ in range(4):
            first._record_failure("coding", "groq", 1200, RuntimeError("timeout"))
        for _ in range(3):
            first._record_success("coding", "gemini", 250)

        restarted = LLMRouter(settings, storage=storage)
        self.assertEqual(restarted.route_for("coding")[0], "gemini")
        debug = restarted.route_debug("coding")
        gemini = next(item for item in debug["providers"] if item["provider"] == "gemini")
        self.assertEqual(gemini["successes"], 3)

    def test_delivery_trace_avoids_raw_prompt_and_response(self):
        storage = Storage()
        traces = LearningTraceStore(storage)
        traces.record_delivery("coding", {
            "provider": "ollama",
            "model": "code-local",
            "raw": "segredo que nao deve entrar no rastro",
            "evaluation": {"score": 91},
            "confidence": {"score": 0.8},
            "code_validation": {"ok": True},
            "tools_used": ["pytest"],
        })
        snapshot = traces.snapshot()
        self.assertEqual(snapshot["trace_count"], 1)
        self.assertNotIn("segredo", str(snapshot))
        self.assertEqual(snapshot["recent"][0]["quality_score"], 91.0)

    def test_model_catalog_merges_runtime_models(self):
        catalog = ModelCatalog(Settings(OLLAMA_BASE_URL="http://localhost:11434"))
        catalog.register_discovered("ollama", ["custom-coder:latest"])
        models = catalog.snapshot()["models"]
        custom = next(item for item in models if item["model_id"] == "custom-coder:latest")
        self.assertTrue(custom["local"])
        self.assertTrue(custom["discovered"])
        self.assertTrue(custom["configured"])

    def test_router_learns_from_delivery_quality(self):
        storage = Storage()
        settings = Settings(LLM_MODE="providers", GROQ_API_KEY="g", GEMINI_API_KEY="m")
        traces = LearningTraceStore(storage)
        for _ in range(8):
            traces.record_delivery("coding", {
                "provider": "groq", "model": "a", "evaluation": {"score": 30},
                "confidence": {"score": 0.3}, "code_validation": {"ok": False},
            })
            traces.record_delivery("coding", {
                "provider": "gemini", "model": "b", "evaluation": {"score": 95},
                "confidence": {"score": 0.9}, "code_validation": {"ok": True},
            })
        router = LLMRouter(settings, storage=storage)
        self.assertEqual(router.route_for("coding")[0], "gemini")
        debug = router.route_debug("coding")
        gemini = next(item for item in debug["providers"] if item["provider"] == "gemini")
        self.assertEqual(gemini["quality"]["samples"], 8)
        self.assertEqual(gemini["quality"]["avg_quality_score"], 95.0)

    def test_human_feedback_is_private_and_idempotent(self):
        storage = Storage()
        traces = LearningTraceStore(storage)
        self.assertTrue(traces.record_feedback("job-1", "coding", "ollama", "coder", "helpful"))
        self.assertFalse(traces.record_feedback("job-1", "coding", "ollama", "coder", "unhelpful"))
        quality = traces.quality_metrics("coding", "ollama")
        self.assertEqual(quality["helpful_feedback"], 1)
        self.assertEqual(quality["unhelpful_feedback"], 0)
        self.assertNotIn("prompt", str(storage.get_json("learning:feedback-receipt:job-1")))


if __name__ == "__main__":
    unittest.main()
