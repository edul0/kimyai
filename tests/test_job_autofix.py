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


if __name__ == "__main__":
    unittest.main()
