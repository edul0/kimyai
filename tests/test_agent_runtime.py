import unittest

from app.agent_runtime import (
    ImpactGraph,
    LocalSemanticSearch,
    SolutionHistory,
    confidence_from_evidence,
)
from app.evaluation import DeliveryEvaluator
from app.frontend_runtime import FrontendBrowserRuntime
from app.storage import Storage


class AgentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_semantic_search_prioritizes_meaningful_document(self):
        docs = [
            {"path": "auth.py", "content": "login token password session authentication"},
            {"path": "billing.py", "content": "invoice total price tax payment"},
        ]
        ranked = LocalSemanticSearch().rank("corrija o token da sessao de login", docs)
        self.assertEqual(ranked[0]["path"], "auth.py")

    def test_impact_graph_finds_reverse_consumer(self):
        graph = ImpactGraph().build(
            {"app/api.py": ["app.service"], "app/worker.py": ["app.queue"]},
            [{"name": "save", "path": "app/service.py"}],
        )
        impacted = ImpactGraph().impacted_files(["app/service.py"], graph)
        self.assertIn("app/api.py", impacted)

    def test_confidence_never_claims_high_without_runtime_evidence(self):
        confidence = confidence_from_evidence(static_ok=True)
        self.assertEqual(confidence.level, "low")
        self.assertIn("navegador-real", confidence.unverified)

    def test_solution_history_keeps_verified_project_memory(self):
        storage = Storage()
        history = SolutionHistory(storage)
        history.record("project", {"fingerprint": "a", "request": "corrigir login token", "success": True})
        found = history.relevant("project", "erro no token de login")
        self.assertEqual(found[0]["fingerprint"], "a")

    async def test_browser_runtime_reports_missing_artifact(self):
        report = await FrontendBrowserRuntime().inspect("sem artifact")
        self.assertFalse(report.available)
        self.assertFalse(report.ok)

    def test_delivery_evaluator_labels_unverified_delivery(self):
        result = {
            "raw": '<kemy_artifact><file path="app/a.py">x=1</file></kemy_artifact>',
            "code_validation": {"ok": True, "attempts": []},
            "browser_validation": {"available": False, "ok": False},
            "confidence": {"score": 0.45},
        }
        evaluation = DeliveryEvaluator().evaluate(result)
        self.assertLess(evaluation["score"], 85)


if __name__ == "__main__":
    unittest.main()
