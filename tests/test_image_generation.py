import unittest

from app.config import Settings
from app.pollinations import PollinationsImageService


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.service = PollinationsImageService(Settings(_env_file=None))

    def test_visual_brief_preserves_literal_request_and_infers_portrait(self):
        prompt = "Crie uma foto realista vertical de uma chef chamada Aurora para stories"
        brief = self.service.build_visual_brief(prompt)
        self.assertIn(prompt, brief["enhanced_prompt"])
        self.assertEqual(brief["size"], "1024x1792")
        self.assertEqual(brief["medium"], "high-end editorial photography")
        self.assertFalse(brief["text_required"])

    def test_visual_brief_avoids_random_text_when_not_requested(self):
        brief = self.service.build_visual_brief("Uma floresta em aquarela ao amanhecer")
        self.assertIn("No text", brief["enhanced_prompt"])
        self.assertEqual(brief["medium"], "expressive watercolor illustration")
        self.assertFalse(brief["text_required"])

    def test_invalid_image_payload_is_rejected(self):
        validation = self.service._validate_image_payload("not-base64", {"size": "1024x1024"})
        self.assertFalse(validation["ok"])


if __name__ == "__main__":
    unittest.main()
