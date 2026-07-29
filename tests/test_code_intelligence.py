import tempfile
import unittest
from pathlib import Path

from app.code_intelligence import RepositoryIntelligence, SafeCodeValidator, reasoning_budget


class RepositoryIntelligenceTests(unittest.TestCase):
    def test_context_prioritizes_requested_file_and_detects_stack(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app").mkdir()
            (root / "tests").mkdir()
            (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
            (root / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
            (root / "app" / "billing.py").write_text("def calculate_total(): return 10\n", encoding="utf-8")
            (root / "tests" / "test_billing.py").write_text("def test_total(): pass\n", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "billing.ts").write_text(
                "import { api } from './api';\nexport function formatInvoice(value: number) { return api(value); }\n",
                encoding="utf-8",
            )
            context = RepositoryIntelligence(root).build_context("corrija calculate_total em billing")
            self.assertIn("FastAPI", context.stack)
            self.assertEqual(context.files[0]["path"], "app/billing.py")
            self.assertIn("python -m pytest -q", context.commands)
            self.assertTrue(any(item["name"] == "calculate_total" for item in context.symbols))
            self.assertIn("app/main.py", context.dependencies)
            self.assertIn("tests/test_billing.py", context.related_tests)
            self.assertTrue(any(item["name"] == "formatInvoice" for item in context.symbols))

    def test_validator_accepts_valid_python_artifact(self):
        raw = (
            '<kemy_artifact title="patch">'
            '<file path="app/service.py">def add(a: int, b: int) -&gt; int:\n    return a + b</file>'
            '<file path="tests/test_service.py">from app.service import add\n\ndef test_add():\n    assert add(1, 2) == 3</file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(raw)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any(item["name"] == "python-compileall" for item in report.checks))

    def test_validator_rejects_syntax_error_and_path_escape(self):
        raw = (
            '<kemy_artifact title="bad">'
            '<file path="../outside.py">print("unsafe")</file>'
            '<file path="app/broken.py">def broken(:\n    pass</file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(raw)
        self.assertFalse(report.ok)
        self.assertTrue(any("inseguro" in item for item in report.errors))
        self.assertTrue(any("SyntaxError" in item for item in report.errors))

    def test_validator_rejects_invented_path_during_maintenance(self):
        raw = (
            '<kemy_artifact title="patch">'
            '<file path="app/invented_service.py">def run():\n    return True</file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(
            raw, existing_paths={"app/service.py"}, allow_new_files=False
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("inexistente no repositorio" in item for item in report.errors))

    def test_validator_overlays_patch_on_isolated_project_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "app").mkdir()
            (root / "app" / "service.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            raw = (
                '<kemy_artifact title="patch">'
                '<file path="app/service.py">def value():\n    return 2</file>'
                '</kemy_artifact>'
            )
            report = SafeCodeValidator().validate(
                raw,
                existing_paths={"app/service.py"},
                allow_new_files=False,
                base_root=root,
            )
            self.assertTrue(report.ok, report.errors)
            self.assertEqual((root / "app" / "service.py").read_text(encoding="utf-8"), "def value():\n    return 1\n")

    def test_reasoning_budget_scales_with_risk(self):
        self.assertEqual(reasoning_budget("mude o nome da variavel"), 1)
        self.assertEqual(
            reasoning_budget("corrija bug de seguranca na auth e banco, refatore arquitetura e adicione testes"),
            3,
        )

    def test_frontend_validator_detects_missing_asset_and_empty_page(self):
        raw = (
            '<kemy_artifact title="frontend">'
            '<file path="preview.html"><!doctype html><html><head>'
            '<script src="missing.js"></script></head><body></body></html></file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(raw)
        self.assertFalse(report.ok)
        self.assertTrue(any("sem conteudo visivel" in item for item in report.errors))
        self.assertTrue(any("asset local inexistente" in item for item in report.errors))

    def test_frontend_validator_accepts_functional_inline_page(self):
        raw = (
            '<kemy_artifact title="frontend">'
            '<file path="preview.html"><!doctype html><html><body><h1>Kemy</h1>'
            '<button id="run">Executar</button>'
            '<script>document.getElementById("run").addEventListener("click", () =&gt; alert("ok"));</script>'
            '</body></html></file></kemy_artifact>'
        )
        report = SafeCodeValidator().validate(raw)
        self.assertTrue(report.ok, report.errors)
        self.assertFalse(report.warnings)

    def test_design_validator_enforces_explicit_brief(self):
        raw = (
            '<kemy_artifact title="site">'
            '<file path="preview.html"><html><body><h1>Template genérico</h1></body></html></file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(
            raw,
            design_request="Crie um dashboard chamado Aurora com sidebar, busca e a cor #6D28D9",
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("#6D28D9" in item for item in report.errors))
        self.assertTrue(any("sidebar" in item for item in report.errors))
        self.assertTrue(any("Aurora" in item for item in report.errors))

    def test_design_validator_accepts_matching_brief(self):
        raw = (
            '<kemy_artifact title="site"><file path="preview.html">'
            '<html><head><style>:root{--brand:#6D28D9}aside{display:block}</style></head>'
            '<body><aside class="sidebar">Aurora</aside><input type="search" placeholder="Buscar"></body></html>'
            '</file></kemy_artifact>'
        )
        report = SafeCodeValidator().validate(
            raw,
            design_request="Crie um dashboard chamado Aurora com sidebar, busca e a cor #6D28D9",
        )
        self.assertTrue(report.ok, report.errors)

    def test_validator_requires_test_for_maintenance_patch(self):
        raw = (
            '<kemy_artifact title="patch">'
            '<file path="app/service.py">def value():\n    return 2</file>'
            '</kemy_artifact>'
        )
        report = SafeCodeValidator().validate(raw, require_tests=True)
        self.assertFalse(report.ok)
        self.assertTrue(any("sem teste relacionado" in item for item in report.errors))


if __name__ == "__main__":
    unittest.main()
