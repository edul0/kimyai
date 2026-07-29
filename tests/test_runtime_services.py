import tempfile
import unittest
from pathlib import Path

from local_app.professional_artifacts import (
    build_docx,
    build_logo_kit,
    build_logo_svg,
    build_pdf,
    build_pptx,
    validate_artifact,
)
from local_app.runtime_services import ActivityTracker, CredentialVault, migrate_env_secrets


class RuntimeServicesTests(unittest.TestCase):
    def test_activity_tracker_keeps_safe_recent_metadata(self):
        tracker = ActivityTracker(limit=3)
        tracker.record("model", "nvidia · model", ms=120, token="do-not-store")
        tracker.record("file", "index.html", lines=20)
        data = tracker.snapshot()
        self.assertEqual(data["last_model"]["label"], "nvidia · model")
        self.assertNotIn("token", data["items"][0])

    def test_windows_vault_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = CredentialVault(Path(tmp) / "credentials.dpapi")
            try:
                vault.update({"API_KEY": "secret-value"})
            except OSError:
                self.skipTest("DPAPI não disponível")
            self.assertEqual(vault.load()["API_KEY"], "secret-value")

    def test_legacy_env_secrets_are_migrated_without_plaintext_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = root / ".env"
            env.write_text("API_KEY=secret-value\nTHEME=dark\n", encoding="utf-8")
            vault = CredentialVault(root / "credentials.dpapi")
            try:
                count = migrate_env_secrets(env, {"API_KEY"}, vault)
            except OSError:
                self.skipTest("DPAPI não disponível")
            self.assertEqual(count, 1)
            self.assertNotIn("secret-value", env.read_text(encoding="utf-8"))
            self.assertEqual(vault.load()["API_KEY"], "secret-value")

    def test_logo_svg_is_valid_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "logo.svg"
            self.assertTrue(build_logo_svg({"brand": "Kemy Labs"}, dest))
            self.assertIn("<svg", dest.read_text(encoding="utf-8"))
            self.assertIn("Kemy Labs", dest.read_text(encoding="utf-8"))

    def test_office_generators_create_files_when_dependencies_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deck = root / "deck.pptx"
            doc = root / "doc.docx"
            ppt_ok = build_pptx({"slides": [{"title": "Visão", "bullets": ["Resultado"]}]}, deck)
            doc_ok = build_docx({"title": "Relatório", "sections": [{"title": "Resumo", "paragraphs": ["Texto"]}]}, doc)
            if not (ppt_ok and doc_ok):
                self.skipTest("Dependências Office não instaladas")
            self.assertGreater(deck.stat().st_size, 1000)
            self.assertGreater(doc.stat().st_size, 1000)
            self.assertTrue(validate_artifact(deck)["ok"])
            self.assertTrue(validate_artifact(doc)["ok"])

    def test_pdf_and_logo_kit_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = {"title": "Relatório", "brand": "Kemy Labs",
                    "sections": [{"title": "Resumo", "paragraphs": ["Conteúdo profissional."]}]}
            pdf = root / "documento.pdf"
            if not build_pdf(spec, pdf):
                self.skipTest("ReportLab não instalado")
            self.assertTrue(validate_artifact(pdf)["ok"])
            kit = build_logo_kit({"brand": "Kemy Labs"}, root / "brand")
            self.assertTrue(any(p.name == "kit-logo.zip" for p in kit))
            self.assertTrue(all(validate_artifact(p)["ok"] for p in kit))


if __name__ == "__main__":
    unittest.main()
