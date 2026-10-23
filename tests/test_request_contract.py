import unittest

from app.request_contract import build_request_contract, evaluate_contract


class RequestContractTests(unittest.TestCase):
    def test_extracts_literal_design_and_functional_requirements(self):
        contract = build_request_contract(
            'Crie um dashboard chamado Aurora com sidebar, busca, modo escuro e cor #6D28D9. '
            'Mantenha o login e nunca remova os filtros.'
        )
        self.assertIn("Aurora", contract.exact_terms)
        self.assertIn("#6D28D9", contract.colors)
        self.assertIn("sidebar", contract.features)
        self.assertIn("busca", contract.features)
        self.assertIn("modo escuro", contract.features)
        self.assertTrue(contract.constraints)

    def test_reports_missing_requirements_without_guessing(self):
        contract = build_request_contract("Crie o painel Aurora com sidebar, busca e #6D28D9")
        report = evaluate_contract(contract, "<aside class='sidebar'>Aurora</aside>")
        self.assertLess(report["score"], 1.0)
        self.assertIn("busca", report["missing"])
        self.assertIn("#6D28D9", report["missing"])

    def test_empty_contract_is_neutral(self):
        report = evaluate_contract(build_request_contract("melhore isso"), "feito")
        self.assertEqual(report["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
