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
        "openrouter": "openrouter/free",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.openrouter_free_state = {
            "available": None,
            "message": "OpenRouter free ainda nao foi testado nesta sessao.",
            "last_error": "",
        }

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
                    result = await self._groq(prompt, current, mode)
                elif provider == "gemini":
                    result = await self._gemini(prompt, current, mode)
                elif provider == "cerebras":
                    result = await self._cerebras(prompt, current, mode)
                elif provider == "openrouter":
                    result = await self._openrouter(prompt, current, mode)
                else:
                    continue
                result = self._apply_provider_notice(result, provider)
                result["fallback_chain"] = attempts + [{"provider": provider, "status": "ok"}]
                return result
            except Exception as exc:
                self._track_provider_failure(provider, exc)
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
        offline = self._apply_provider_notice(offline, "mock")
        offline["fallback_chain"] = attempts + [{"provider": "mock", "status": "ok"}]
        return offline

    def _mock_response(self, prompt: str, mode: str, choice: ProviderChoice) -> dict[str, Any]:
        summary = textwrap.shorten(" ".join(prompt.split()), width=260, placeholder="...")
        casual = "Conversa casual: sim" in prompt
        if casual:
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": "Oi. Estou aqui. Me diga o que voce quer construir, revisar ou melhorar.",
                "summary": "Resposta casual.",
                "files": [],
                "diff": "",
                "tests": [],
            }
        if mode == "site":
            html = (
                "<!doctype html>\n"
                "<html lang=\"pt-BR\">\n"
                "<head>\n"
                "  <meta charset=\"utf-8\" />\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
                "  <title>Kemy Preview</title>\n"
                "  <style>\n"
                "    body { margin: 0; font-family: Georgia, serif; background: #f7f4ee; color: #151515; }\n"
                "    main { min-height: 100vh; display: grid; place-items: center; padding: 48px; }\n"
                "    section { max-width: 920px; background: white; border-radius: 28px; padding: 48px; box-shadow: 0 24px 80px rgba(0,0,0,.08); }\n"
                "    h1 { font-size: 64px; line-height: 1; margin: 0 0 18px; }\n"
                "    p { font: 18px/1.6 ui-sans-serif, system-ui, sans-serif; color: #4a4a4a; }\n"
                "    button { margin-top: 18px; border: 0; border-radius: 999px; padding: 14px 22px; background: #151515; color: #fff; font-weight: 700; }\n"
                "  </style>\n"
                "</head>\n"
                "<body>\n"
                "  <main>\n"
                "    <section>\n"
                "      <h1>Site funcional em preview</h1>\n"
                "      <p>Este mock local mostra como a Kemy pode devolver um HTML completo para live preview e depois refinar para estilo Manus ou Firebase Studio.</p>\n"
                "      <button>Continuar refinando</button>\n"
                "    </section>\n"
                "  </main>\n"
                "</body>\n"
                "</html>"
            )
            raw = (
                "Segue um `index.html` inicial para preview imediato.\n\n"
                "```html\n"
                f"{html}\n"
                "```"
            )
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": raw,
                "summary": "Preview HTML inicial criado.",
                "files": [{"path": "index.html", "language": "html", "content": html}],
                "diff": raw,
                "tests": ["Abra o preview ao lado para validar o layout base."],
            }
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

    async def _groq(self, prompt: str, choice: ProviderChoice, mode: str) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.groq_api_key}"}
        system = self._system_prompt("groq", mode)
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    async def _gemini(self, prompt: str, choice: ProviderChoice, mode: str) -> dict[str, Any]:
        from google import genai

        client = genai.Client(api_key=self.settings.gemini_api_key)
        response = client.models.generate_content(model=choice.model, contents=f"{self._system_prompt('gemini', mode)}\n\n{prompt}")
        return {"provider": choice.name, "model": choice.model, "raw": response.text, "files": [], "diff": response.text}

    async def _cerebras(self, prompt: str, choice: ProviderChoice, mode: str) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.cerebras_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": self._system_prompt("cerebras", mode)},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.cerebras.ai/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    async def _openrouter(self, prompt: str, choice: ProviderChoice, mode: str) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.openrouter_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": self._system_prompt("openrouter", mode)},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    def _system_prompt(self, provider: str, mode: str) -> str:
        base = (
            "Voce e um engenheiro senior de produto e coding. Responda em Markdown, nunca em JSON cru. "
            "Nao invente preset. Nao troque FastAPI por Flask, React por Vue, ou outra stack salvo se o usuario pedir. "
            "Entregue diagnostico, arquivos afetados, codigo/patch e testes."
        )
        if provider == "cerebras":
            base = "Voce e um auditor tecnico rapido e preciso. Responda em Markdown claro, sem JSON cru."
        if mode != "site":
            return base
        return (
            f"{base} "
            "Se o pedido for de site, landing page ou interface visual, devolva obrigatoriamente um bloco ```html``` completo e funcional, "
            "de preferencia com CSS e JS inline no mesmo arquivo para permitir live preview imediato."
        )

    def _track_provider_failure(self, provider: str, exc: Exception) -> None:
        if provider != "openrouter":
            return
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        message = str(exc)
        if status_code in {402, 429, 503}:
            self.openrouter_free_state = {
                "available": False,
                "message": self._openrouter_failure_message(status_code),
                "last_error": message[:240],
            }

    def _apply_provider_notice(self, result: dict[str, Any], provider: str) -> dict[str, Any]:
        if provider == "openrouter":
            previous = self.openrouter_free_state.get("available")
            if previous is False:
                notice = "Aviso de sistema: o OpenRouter gratuito voltou a responder agora."
                result["raw"] = f"{notice}\n\n{result.get('raw', '')}".strip()
                result["summary"] = f"{notice} {result.get('summary', '')}".strip()
            self.openrouter_free_state = {
                "available": True,
                "message": "OpenRouter free ativo.",
                "last_error": "",
            }
            return result
        if self.openrouter_free_state.get("available") is False:
            notice = self.openrouter_free_state.get("message") or "OpenRouter free indisponivel."
            result["raw"] = f"{notice}\n\n{result.get('raw', '')}".strip()
            result["summary"] = f"{notice} {result.get('summary', '')}".strip()
        return result

    def _openrouter_failure_message(self, status_code: int | None) -> str:
        messages = {
            402: "Aviso de sistema: o OpenRouter gratuito foi encerrado por falta de creditos ou saldo abaixo de zero. A Kemy segue usando as outras rotas gratuitas.",
            429: "Aviso de sistema: o OpenRouter gratuito foi encerrado temporariamente por limite de uso. A Kemy segue com as outras rotas e tenta novamente depois.",
            503: "Aviso de sistema: o OpenRouter gratuito ficou indisponivel agora. A Kemy continua com os outros provedores gratuitos.",
        }
        return messages.get(status_code, "Aviso de sistema: o OpenRouter gratuito ficou indisponivel temporariamente.")
