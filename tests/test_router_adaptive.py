import unittest

from app.config import Settings
from app.llm_router import LLMRouter


class AdaptiveRouterTests(unittest.TestCase):
    def test_route_reorders_after_failures(self):
        settings = Settings(
            LLM_MODE="providers",
            GROQ_API_KEY="g",
            GEMINI_API_KEY="m",
            CEREBRAS_API_KEY="c",
            OPENROUTER_API_KEY="o",
        )
        router = LLMRouter(settings)
        initial = router.route_for("coding")
        self.assertEqual(initial[0], "groq")

        for _ in range(4):
            router._record_failure("coding", "groq", 1200, RuntimeError("timeout"))
        for _ in range(3):
            router._record_success("coding", "gemini", 350)

        reordered = router.route_for("coding")
        self.assertEqual(reordered[0], "gemini")

    def test_route_debug_returns_metrics(self):
        settings = Settings(
            LLM_MODE="providers",
            GROQ_API_KEY="g",
            GEMINI_API_KEY="m",
        )
        router = LLMRouter(settings)
        router._record_success("coding", "groq", 220)
        debug = router.route_debug("coding")
        self.assertEqual(debug["mode"], "coding")
        self.assertTrue(debug["providers"])
        groq = next(item for item in debug["providers"] if item["provider"] == "groq")
        self.assertGreaterEqual(groq["attempts"], 1)
        self.assertGreaterEqual(groq["avg_latency_ms"], 0)


if __name__ == "__main__":
    unittest.main()
