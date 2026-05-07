import base64
import io
import unittest

from PIL import Image

from app.attachment_service import prepare_attachments


class AttachmentServiceTests(unittest.TestCase):
    def test_image_attachment_generates_visual_summary(self):
        image = Image.new("RGB", (80, 50), (12, 24, 38))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        attachment = {
            "name": "referencia.png",
            "mime_type": "image/png",
            "content": f"data:image/png;base64,{payload}",
            "kind": "image",
        }

        prepared = prepare_attachments([attachment])

        self.assertTrue(prepared["has_visual"])
        self.assertIn("Resumo visual local do anexo", prepared["prompt_context"])
        first = prepared["items"][0]
        self.assertIn("image", first["notes"])
        self.assertIn("image-brief", first["notes"])
        self.assertTrue(first["text"])
        self.assertEqual(first["metadata"].get("orientation"), "paisagem")


if __name__ == "__main__":
    unittest.main()
