from __future__ import annotations

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

        payload = {
            "model": self.settings.pollinations_image_model,
            "prompt": clean_prompt,
            "size": self.settings.pollinations_image_size,
            "quality": self.settings.pollinations_image_quality,
            "response_format": "url",
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

        image_url = ((data.get("data") or [{}])[0]).get("url")
        if not image_url:
            raise RuntimeError("Pollinations nao retornou URL de imagem.")

        auth_mode = "key" if self.settings.pollinations_api_key else "anon"
        summary = f'Imagem gerada para: "{clean_prompt}"'
        raw = f"{summary}\n\nURL: {image_url}"
        if auth_mode == "anon":
            raw = f"{summary}\n\nGerada em modo gratuito anonimo.\nURL: {image_url}"

        return {
            "provider": "pollinations",
            "model": self.settings.pollinations_image_model,
            "summary": summary,
            "raw": raw,
            "image_url": image_url,
            "prompt": clean_prompt,
            "auth_mode": auth_mode,
            "files": [],
            "diff": "",
            "tests": [],
        }
