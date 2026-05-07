import unittest

from app.config import Settings
from app.document_service import DocumentService
from app.intent_planner import build_execution_plan, classify_request_mode, is_slide_request


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


if __name__ == "__main__":
    unittest.main()
