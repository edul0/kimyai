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

    def test_display_answer_prefers_summary_for_file_deliveries(self):
        manager = JobManager(Storage(), Settings())
        result = {
            "summary": "Projeto entregue com preview.",
            "raw": "<kemy_artifact title=\"Site\">...</kemy_artifact>",
            "files": [{"name": "preview.html", "content": "<html></html>"}],
        }
        answer = manager._display_answer_for_history(result)
        self.assertEqual(answer, "Projeto entregue com preview.")

    def test_compact_result_metadata_removes_heavy_file_content(self):
        manager = JobManager(Storage(), Settings())
        result = {
            "summary": "Entrega pronta",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "files": [
                {
                    "name": "preview.html",
                    "relative_path": "preview.html",
                    "mime_type": "text/html",
                    "download_url": "/api/artefatos/job/preview.html",
                    "language": "html",
                    "content": "<!doctype html><html>...</html>",
                }
            ],
        }
        compact = manager._compact_result_metadata(result, mode="site")
        self.assertEqual(compact["mode"], "site")
        self.assertEqual(compact["files"][0]["name"], "preview.html")
        self.assertNotIn("content", compact["files"][0])


if __name__ == "__main__":
    unittest.main()
