import base64
import unittest

from app.main import _job_payload_from_supabase, _match_remote_artifact, _remote_artifact_response


class PersistentRecoveryTests(unittest.TestCase):
    def test_job_payload_from_supabase_row_matches_job_state_shape(self):
        row = {
            "id": "job-1",
            "session_id": "session-1",
            "status": "done",
            "mode": "site",
            "prompt": "gere um site",
            "progress": 100,
            "stage": "Concluido",
            "result": {"summary": "ok"},
            "events": [{"msg": "feito"}],
            "created_at": "2026-05-07T00:00:00Z",
            "updated_at": "2026-05-07T00:00:01Z",
        }
        payload = _job_payload_from_supabase(row)
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["modo"], "site")
        self.assertEqual(payload["resultado"], {"summary": "ok"})

    def test_remote_artifact_match_accepts_safe_download_filename(self):
        rows = [
            {"path": "src/App.tsx", "name": "src__App.tsx"},
            {"path": "preview.html", "name": "preview.html"},
        ]
        self.assertEqual(_match_remote_artifact(rows, "src__App.tsx")["path"], "src/App.tsx")
        self.assertEqual(_match_remote_artifact(rows, "preview.html")["path"], "preview.html")

    def test_remote_artifact_response_serves_base64_bytes(self):
        row = {
            "mime_type": "application/pdf",
            "content_base64": base64.b64encode(b"%PDF-test").decode("ascii"),
        }
        response = _remote_artifact_response(row, "arquivo.pdf")
        self.assertEqual(response.media_type, "application/pdf")
        self.assertEqual(response.body, b"%PDF-test")


if __name__ == "__main__":
    unittest.main()
