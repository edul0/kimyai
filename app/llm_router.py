from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Any

from .config import Settings


@dataclass(frozen=True)
class ProviderChoice:
    name: str
    model: str
    reason: str


class LLMRouter:
    """Chooses free-tier providers and keeps paid fallbacks opt-in."""

    ROUTES = {
        "coding": ["groq", "gemini", "cerebras", "openrouter"],
        "site": ["gemini", "groq", "cerebras", "openrouter"],
        "auditoria": ["cerebras", "groq", "gemini", "openrouter"],
        "planejamento": ["gemini", "groq", "cerebras", "openrouter"],
    }

    MODELS = {
        "groq": "openai/gpt-oss-120b",
        "gemini": "gemini-2.5-flash",
        "cerebras": "llama3.1-70b",
        "openrouter": "meta-llama/llama-3-70b-instruct",
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    def choose(self, mode: str = "coding") -> ProviderChoice:
        for provider in self.route_for(mode):
            return ProviderChoice(provider, self.model_for(provider), self.reason_for(provider, mode))
        return ProviderChoice("mock", "local-planner", "no cloud keys configured")

    def route_for(self, mode: str = "coding") -> list[str]:
        configured = self.settings.configured_providers
        route = self.ROUTES.get(mode, self.ROUTES["coding"])
        return [provider for provider in route if configured.get(provider)]

    def model_for(self, provider: str) -> str:
        if provider == "groq" and self.settings.default_model.startswith("groq/"):
            return self.settings.default_model.replace("groq/", "", 1)
        return self.MODELS[provider]

    def reason_for(self, provider: str, mode: str) -> str:
        reasons = {
            "groq": "melhor rota gratuita para gerar codigo rapido",
            "gemini": "melhor rota gratuita para contexto longo, planejamento e leitura visual",
            "cerebras": "melhor rota gratuita para revisao e auditoria rapida",
            "openrouter": "fallback pago/externo habilitado explicitamente",
        }
        return f"{reasons[provider]} em modo {mode}"

    async def generate(self, prompt: str, mode: str = "coding") -> dict[str, Any]:
        route = self.route_for(mode)
        choice = self.choose(mode)
        if self.settings.llm_mode != "providers" or not route:
            return self._mock_response(prompt, mode, choice)

        attempts = []
        for provider in route:
            current = ProviderChoice(provider, self.model_for(provider), self.reason_for(provider, mode))
            try:
                if provider == "groq":
                    result = await self._groq(prompt, current)
                elif provider == "gemini":
                    result = await self._gemini(prompt, current)
                elif provider == "cerebras":
                    result = await self._cerebras(prompt, current)
                elif provider == "openrouter":
                    result = await self._openrouter(prompt, current)
                else:
                    continue
                result["fallback_chain"] = attempts + [{"provider": provider, "status": "ok"}]
                return result
            except Exception as exc:
                attempts.append(
                    {
                        "provider": provider,
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:240],
                    }
                )
                continue

        offline = self._mock_response(prompt, mode, ProviderChoice("mock", "local-planner", "free providers exhausted"))
        offline["fallback_chain"] = attempts + [{"provider": "mock", "status": "ok"}]
        return offline

    def _mock_response(self, prompt: str, mode: str, choice: ProviderChoice) -> dict[str, Any]:
        summary = textwrap.shorten(" ".join(prompt.split()), width=260, placeholder="...")
        files = [
            {
                "path": "README_IMPLEMENTACAO.md",
                "language": "markdown",
                "content": (
                    "# Plano de implementacao Kemy AI\n\n"
                    f"Modo: {mode}\n\n"
                    f"Resumo do pedido: {summary}\n\n"
                    "Entregaveis recomendados:\n"
                    "- quebrar a tarefa em requisitos, arquitetura, frontend, backend, auditoria e QA\n"
                    "- gerar arquivos em formato estruturado antes de responder ao usuario\n"
                    "- validar seguranca, testes e deploy antes do merge\n"
                ),
            }
        ]
        return {
            "provider": choice.name,
            "model": choice.model,
            "reason": choice.reason,
            "summary": summary,
            "files": files,
            "diff": "",
            "tests": ["python -m compileall app agencia_kemy.py"],
            "security_report": "Modo mock: nenhuma chamada externa feita e nenhum segredo lido.",
        }

    async def _groq(self, prompt: str, choice: ProviderChoice) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": "Voce e um agente senior focado em coding. Responda em JSON util."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    async def _gemini(self, prompt: str, choice: ProviderChoice) -> dict[str, Any]:
        from google import genai

        client = genai.Client(api_key=self.settings.gemini_api_key)
        response = client.models.generate_content(model=choice.model, contents=prompt)
        return {"provider": choice.name, "model": choice.model, "raw": response.text, "files": [], "diff": response.text}

    async def _cerebras(self, prompt: str, choice: ProviderChoice) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.cerebras_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": "Voce e um auditor tecnico rapido e preciso."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    async def _openrouter(self, prompt: str, choice: ProviderChoice) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": "Voce e um agente senior focado em coding."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}
