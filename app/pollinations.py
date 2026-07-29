from __future__ import annotations

import base64
import hashlib
import io
import re
from typing import Any

import httpx

from .config import Settings


class PollinationsImageService:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def generate(self, prompt: str) -> dict[str, Any]:
        clean_prompt = " ".join(prompt.split()).strip()
        if not clean_prompt:
            raise ValueError("Prompt de imagem vazio.")
        if not self.settings.pollinations_api_key:
            raise RuntimeError(
                "A rota de imagem da Kemy esta pronta, mas a chave do Pollinations nao foi configurada no servidor. "
                "Adicione POLLINATIONS_API_KEY nas variaveis de ambiente e tente novamente."
            )

        headers = {}
        headers["Authorization"] = f"Bearer {self.settings.pollinations_api_key}"

        visual_brief = self.build_visual_brief(clean_prompt)
        payload = {
            "model": self.settings.pollinations_image_model,
            "prompt": visual_brief["enhanced_prompt"],
            "size": visual_brief["size"],
            "quality": self.settings.pollinations_image_quality,
            "response_format": "b64_json",
            "user": hashlib.sha256(clean_prompt.encode("utf-8")).hexdigest()[:24],
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                "https://gen.pollinations.ai/v1/images/generations",
                headers=headers,
                json=payload,
            )
            if response.status_code == 401:
                raise RuntimeError("Pollinations recusou a autenticacao. Rotacione a chave e atualize POLLINATIONS_API_KEY no servidor.")
            response.raise_for_status()
            data = response.json()

        image_payload = (data.get("data") or [{}])[0]
        image_b64 = image_payload.get("b64_json")
        image_url = image_payload.get("url")
        if not image_b64 and not image_url:
            raise RuntimeError("Pollinations nao retornou imagem utilizavel.")

        image_data_url = f"data:image/png;base64,{image_b64}" if image_b64 else None
        image_validation = self._validate_image_payload(image_b64, visual_brief)

        auth_mode = "key" if self.settings.pollinations_api_key else "anon"
        summary = f'Imagem gerada para: "{clean_prompt}"'
        raw = f"{summary}\n\nImagem pronta para preview na conversa."
        if auth_mode == "anon":
            raw = f"{summary}\n\nGerada em modo gratuito anonimo e pronta para preview."

        return {
            "provider": "pollinations",
            "model": self.settings.pollinations_image_model,
            "summary": summary,
            "raw": raw,
            "image_url": image_url,
            "image_data_url": image_data_url,
            "prompt": clean_prompt,
            "enhanced_prompt": visual_brief["enhanced_prompt"],
            "visual_brief": visual_brief,
            "image_validation": image_validation,
            "auth_mode": auth_mode,
            "files": [],
            "diff": "",
            "tests": [],
        }

    def build_visual_brief(self, prompt: str) -> dict[str, Any]:
        lowered = prompt.lower()
        if any(marker in lowered for marker in ("story", "stories", "retrato", "portrait", "vertical", "9:16")):
            size, composition = "1024x1792", "vertical portrait composition, clear foreground-middle-background hierarchy"
        elif any(marker in lowered for marker in ("banner", "capa", "hero", "paisagem", "landscape", "wide", "16:9")):
            size, composition = "1792x1024", "wide cinematic composition, strong focal point with intentional negative space"
        else:
            size, composition = self.settings.pollinations_image_size, "balanced composition with one unmistakable focal point"

        medium = "professional digital illustration"
        medium_map = (
            (("foto", "fotografia", "realista", "photoreal"), "high-end editorial photography"),
            (("logo", "icone", "ícone", "vetor"), "clean vector identity design"),
            (("anime", "manga"), "premium anime key visual"),
            (("3d", "render"), "polished cinematic 3D render"),
            (("aquarela",), "expressive watercolor illustration"),
            (("pixel art",), "crisp handcrafted pixel art"),
        )
        for markers, value in medium_map:
            if any(marker in lowered for marker in markers):
                medium = value
                break

        text_rule = (
            "If typography is requested, render only the exact requested words, perfectly spelled and legible."
            if any(marker in lowered for marker in ("texto", "escrito", "nome", "logo", "titulo", "título"))
            else "No text, letters, captions, watermarks, logos or signatures."
        )
        enhanced = (
            f"USER INTENT (must be followed literally): {prompt}. "
            f"Art direction: {medium}; {composition}; coherent color palette derived from the request; "
            "intentional lighting; convincing materials and depth; strong visual hierarchy; precise anatomy and geometry; "
            "clean edges where appropriate; no generic stock-image composition. "
            f"{text_rule} "
            "Avoid: low resolution, blur, artifacts, duplicated objects, malformed hands, extra fingers, distorted faces, "
            "cropped subject, random text, watermark, muddy colors, clutter, generic template aesthetics."
        )
        return {
            "original_prompt": prompt,
            "enhanced_prompt": enhanced,
            "medium": medium,
            "composition": composition,
            "size": size,
            "text_required": "If typography is requested" in text_rule,
        }

    @staticmethod
    def _validate_image_payload(image_b64: str | None, brief: dict[str, Any]) -> dict[str, Any]:
        if not image_b64:
            return {"ok": True, "format": "remote-url", "warnings": ["Dimensoes nao verificadas porque o provedor retornou URL."]}
        try:
            from PIL import Image

            raw = base64.b64decode(image_b64, validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                image_format = image.format or "unknown"
            requested = re.match(r"(\d+)x(\d+)", str(brief.get("size") or ""))
            warnings = []
            if requested:
                expected_ratio = int(requested.group(1)) / int(requested.group(2))
                actual_ratio = width / max(height, 1)
                if abs(expected_ratio - actual_ratio) > 0.08:
                    warnings.append("Proporcao retornada diverge do formato solicitado.")
            return {
                "ok": width >= 512 and height >= 512 and not warnings,
                "format": image_format,
                "width": width,
                "height": height,
                "warnings": warnings,
            }
        except Exception as exc:
            return {"ok": False, "warnings": [f"Imagem retornada nao pode ser validada: {type(exc).__name__}."]}
