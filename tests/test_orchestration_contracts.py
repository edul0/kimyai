import unittest

from app.config import Settings
from app.intent_planner import build_execution_plan, classify_request_mode
from app.jobs import JobManager
from app.schemas import JobState
from app.storage import Storage


class OrchestrationContractsTests(unittest.TestCase):
    def test_daily_request_routes_to_planning_mode(self):
        mode = classify_request_mode("organize um checklist para minha rotina de estudos", "coding")
        self.assertEqual(mode, "planejamento")

    def test_slide_plan_declares_document_kind(self):
        plan = build_execution_plan("crie slides sobre gestao de ativos de TI", "coding")
        self.assertEqual(plan.mode, "documento")
        self.assertEqual(plan.document_kind, "slides")
        self.assertIn("slides", plan.response_contract.lower())

    def test_register_user_turn_prevents_duplicate_user_message(self):
        manager = JobManager(Storage(), Settings())
        session_id = "session-contract-test"
        pedido = "gere um docx sobre governanca de TI"
        manager.register_user_turn(session_id, pedido, owner="user@example.com")
        session_data, added_user, _user_entry, _assistant_entry = manager._append_history(
            session_id,
            pedido,
            {"summary": "Documento pronto."},
        )
        history = session_data.get("historico", [])
        self.assertFalse(added_user)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].get("role"), "user")
        self.assertEqual(history[1].get("role"), "assistant")

    def test_response_cache_roundtrip(self):
        manager = JobManager(Storage(), Settings())
        job = JobState(
            job_id="job-cache",
            session_id="session-cache",
            status="running",
            etapa="teste",
            progresso=50,
            pedido="corrija endpoint de login",
            modo="coding",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
        )
        key = manager._response_cache_key(
            job,
            {"intent": "coding", "response_contract": "markdown"},
            {"summary": "usuario quer robustez"},
            [],
        )
        manager._set_cached_response(key, {"raw": "ok", "summary": "cacheado"})
        cached = manager._get_cached_response(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["summary"], "cacheado")


if __name__ == "__main__":
    unittest.main()
