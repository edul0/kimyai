import unittest

from app.config import Settings
from app.document_service import DocumentService
from app.jobs import JobManager
from app.intent_planner import build_execution_plan, classify_request_mode, is_slide_request
from app.schemas import JobState
from app.storage import Storage


class IntentRoutingTests(unittest.TestCase):
    def test_site_request_routes_to_site(self):
        plan = build_execution_plan("gere um site para agendar cortes no barbeiro", "coding")
        self.assertEqual(plan.mode, "site")
        self.assertEqual(plan.preview_file, "preview.html")
        self.assertIn("preview.html", plan.expected_files)

    def test_docx_abnt_request_routes_to_document_not_slide(self):
        pedido = "me gera um docxs em formato abnt sobre as chances de ocorrer uma possivel terceira guerra mundial"
        self.assertEqual(classify_request_mode(pedido, "coding"), "documento")
        plan = build_execution_plan(pedido, "coding")
        self.assertEqual(plan.mode, "documento")
        self.assertEqual(plan.intent, "documento executivo")

    def test_slide_request_routes_to_document_slide_intent(self):
        pedido = "crie um slide sobre gestao de ativos"
        self.assertTrue(is_slide_request(pedido))
        plan = build_execution_plan(pedido, "coding")
        self.assertEqual(plan.mode, "documento")
        self.assertEqual(plan.intent, "apresentacao/slides")
        self.assertEqual(plan.preview_file, "slides.html")

    def test_code_request_stays_coding(self):
        plan = build_execution_plan("corrija o endpoint FastAPI que salva conversas no Supabase", "coding")
        self.assertEqual(plan.mode, "coding")
        self.assertIn("FastAPI", plan.stack)

    def test_site_followup_keeps_site_mode(self):
        session_data = {
            "historico": [
                {
                    "role": "assistant",
                    "result": {"preview_url": "/api/artefatos/job/preview.html"},
                    "files": [{"name": "preview.html"}],
                }
            ]
        }
        self.assertEqual(classify_request_mode("melhore isso e deixe mais bonito", "coding", session_data), "site")

    def test_site_followup_uses_stored_last_mode(self):
        session_data = {"last_mode": "site", "historico": []}
        self.assertEqual(classify_request_mode("deixe mais premium", "coding", session_data), "site")

    def test_document_followup_keeps_document_mode(self):
        session_data = {
            "historico": [
                {
                    "role": "assistant",
                    "result": {"document_title": "Terceira Guerra Mundial"},
                    "files": [{"name": "terceira-guerra-mundial.docx"}],
                }
            ]
        }
        self.assertEqual(classify_request_mode("ajuste isso no formato abnt", "coding", session_data), "documento")

    def test_document_service_rejects_code_dump_as_docx_content(self):
        service = DocumentService(Settings())
        pedido = "me gera um docxs em formato abnt sobre terceira guerra mundial"
        code_dump = """
from docx import Document
RECUO_PRIMEIRA_LINHA = Cm(1.25)
def _apply_abnt_style(paragraph):
    paragraph.paragraph_format.first_line_indent = RECUO_PRIMEIRA_LINHA
    for run in paragraph.runs:
        run.font.name = FONTE
"""
        cleaned = service._normalize_document_text(code_dump, pedido)
        self.assertFalse(service._looks_like_document_content(cleaned))
        self.assertIn("# Terceira Guerra Mundial", service._source_text(pedido, {"raw": code_dump}))

    def test_document_request_does_not_become_slide_from_generated_text(self):
        service = DocumentService(Settings())
        pedido = "gere um docx em formato abnt sobre terceira guerra mundial"
        self.assertFalse(service._is_slide_request(pedido, "## Slide 1\nconteudo gerado"))

    def test_site_html_block_is_repaired_into_artifact(self):
        manager = JobManager(Storage(), Settings())
        job = JobState(
            job_id="job-site-repair",
            session_id="session-site-repair",
            status="running",
            etapa="teste",
            progresso=10,
            pedido="gere um site para barbearia",
            modo="site",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
        )
        result = {
            "raw": "```html\n<!doctype html><html><body><h1>Agenda</h1></body></html>\n```",
            "tools_used": [],
        }
        fixed = manager._ensure_site_artifact(job, result)
        self.assertIn("<kemy_artifact", fixed["raw"])
        self.assertIn("<file path=\"preview.html\">", fixed["raw"])
        self.assertIn("kemy-site-html-repair", fixed["tools_used"])

    def test_vite_shell_is_not_accepted_as_live_preview(self):
        manager = JobManager(Storage(), Settings())
        vite_shell = """
<!doctype html>
<html lang="pt-BR">
  <head><meta charset="utf-8" /><title>App</title></head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
"""
        self.assertFalse(manager._is_renderable_preview_html(vite_shell))

    def test_vite_shell_falls_back_to_functional_preview(self):
        manager = JobManager(Storage(), Settings())
        job = JobState(
            job_id="job-site-shell",
            session_id="session-site-shell",
            status="running",
            etapa="teste",
            progresso=10,
            pedido="crie um site sobre agendar horario no barbeiro",
            modo="site",
            created_at="2026-05-07T00:00:00Z",
            updated_at="2026-05-07T00:00:00Z",
        )
        raw = """
<kemy_artifact title="BarberShop Booking System">
<file path="index.html">
<!doctype html>
<html><body><div id="root"></div><script type="module" src="/src/main.tsx"></script></body></html>
</file>
</kemy_artifact>
"""
        fixed = manager._ensure_site_artifact(job, {"raw": raw, "tools_used": []})
        self.assertIn("kemy-site-fallback", fixed["tools_used"])
        self.assertIn("<file path=\"preview.html\">", fixed["raw"])


if __name__ == "__main__":
    unittest.main()
