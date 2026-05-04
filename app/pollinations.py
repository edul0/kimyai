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
            "response_format": "b64_json",
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
            "auth_mode": auth_mode,
            "files": [],
            "diff": "",
            "tests": [],
        }
