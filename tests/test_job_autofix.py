import unittest

from app.config import Settings
from app.jobs import JobManager
from app.storage import Storage


class JobAutofixHeuristicsTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager(Storage(), Settings())

    def test_site_self_review_reason_detects_weak_output(self):
        result = {"summary": "Concluido.", "files": []}
        reason = self.manager._self_review_reason("site", result, "Concluido.")
        self.assertEqual(reason, "saida-site-fraca")

    def test_coding_self_review_reason_detects_too_short(self):
        result = {"summary": "Feito.", "files": [], "diff": ""}
        reason = self.manager._self_review_reason("coding", result, "Feito.")
        self.assertEqual(reason, "saida-coding-curta")

    def test_review_candidate_site_accepts_artifact_upgrade(self):
        better = self.manager._is_review_candidate_better("site", "Concluido.", "<kemy_artifact title=\"x\"></kemy_artifact>")
        self.assertTrue(better)

    def test_engineering_review_accepts_more_verifiable_code(self):
        original = "Altere a funcao e pronto."
        candidate = (
            "Arquivo afetado: `app/service.py`.\n```python\n"
            "def load():\n    try:\n        return repository.load()\n"
            "    except TimeoutError:\n        return fallback()\n```\n"
            "Preserva o contrato existente e evita regressao. Teste com `pytest -q`."
        )
        self.assertTrue(self.manager._is_review_candidate_better("coding", original, candidate))

    def test_engineering_review_rejects_verbose_placeholder(self):
        original = "Arquivo corrigido com validacao, compatibilidade e teste executavel via pytest."
        candidate = ("TODO: adicione aqui o restante do codigo. " * 30).strip()
        self.assertFalse(self.manager._is_review_candidate_better("coding", original, candidate))

    def test_site_edit_context_reuses_previous_project(self):
        session_data = {
            "historico": [
                {
                    "role": "assistant",
                    "content": "Projeto base pronto.",
                    "result": {
                        "artifact_title": "Barbearia Agenda",
                        "files": [
                            {
                                "name": "preview.html",
                                "content": "<!doctype html><html><body><h1>Barberia</h1><p>Tom inicial.</p></body></html>",
                            },
                            {
                                "name": "src/App.tsx",
                                "content": "export default function App(){return <main>Barbearia Agenda</main>;}",
                            },
                        ],
                    },
                }
            ]
        }
        job = self.manager.create("sid-ctx", "mude o tom do site para premium", "site")
        context = self.manager._build_site_edit_context(job, session_data)
        self.assertIn("Projeto atual:", context)
        self.assertIn("preview.html", context)
        self.assertIn("evolua esse mesmo projeto", context)

    def test_response_cache_key_changes_when_site_context_changes(self):
        job = self.manager.create("sid-cache", "melhore esse site", "site")
        plan = {"intent": "site/app gerado", "response_contract": "Responder apenas com kemy_artifact."}
        key_a = self.manager._response_cache_key(job, plan, {}, [], site_context="site-v1")
        key_b = self.manager._response_cache_key(job, plan, {}, [], site_context="site-v2")
        self.assertNotEqual(key_a, key_b)


if __name__ == "__main__":
    unittest.main()
