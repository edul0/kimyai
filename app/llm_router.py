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
        "coding": ["groq", "gemini", "cerebras", "openrouter", "openai"],
        "site": ["gemini", "groq", "cerebras", "openrouter", "openai"],
        "auditoria": ["cerebras", "groq", "gemini", "openrouter", "openai"],
        "planejamento": ["gemini", "groq", "cerebras", "openrouter", "openai"],
        "documento": ["gemini", "groq", "cerebras", "openrouter", "openai"],
    }

    MODELS = {
        "groq": "openai/gpt-oss-120b",
        "cerebras": "llama3.1-70b",
        "openrouter": "openrouter/free",
        "openai": "gpt-4.1-mini",
    }

    def __init__(self, settings: Settings):
        self.settings = settings
        self.openrouter_free_state = {
            "available": None,
            "message": "OpenRouter free ainda nao foi testado nesta sessao.",
            "last_error": "",
        }
        self.gemini_state = {
            "active_model": settings.gemini_primary_model,
            "fallback_model": settings.gemini_fallback_model,
            "using_fallback": False,
            "message": f"Tentando {settings.gemini_primary_model} primeiro.",
            "last_error": "",
        }

    def choose(self, mode: str = "coding", has_visual: bool = False) -> ProviderChoice:
        for provider in self.route_for(mode, has_visual=has_visual):
            return ProviderChoice(provider, self.model_for(provider), self.reason_for(provider, mode))
        return ProviderChoice("mock", "local-planner", "no cloud keys configured")

    def route_for(self, mode: str = "coding", has_visual: bool = False) -> list[str]:
        configured = self.settings.configured_providers
        route = self.ROUTES.get(mode, self.ROUTES["coding"])
        available = [
            provider
            for provider in route
            if configured.get(provider) and (provider != "openai" or not self.settings.free_only)
        ]
        if has_visual and "gemini" in available:
            return ["gemini"] + [provider for provider in available if provider != "gemini"]
        return available

    def model_for(self, provider: str) -> str:
        if provider == "groq" and self.settings.default_model.startswith("groq/"):
            return self.settings.default_model.replace("groq/", "", 1)
        if provider == "gemini":
            return self.gemini_state["active_model"]
        if provider == "openai":
            return self.settings.openai_model
        return self.MODELS[provider]

    def reason_for(self, provider: str, mode: str) -> str:
        reasons = {
            "groq": "melhor rota gratuita para gerar codigo rapido",
            "gemini": "melhor rota gratuita para contexto longo, planejamento e leitura visual",
            "cerebras": "melhor rota gratuita para revisao e auditoria rapida",
            "openrouter": "fallback pago/externo habilitado explicitamente",
            "openai": "rota oficial da OpenAI habilitada explicitamente fora do modo gratuito",
        }
        return f"{reasons[provider]} em modo {mode}"

    async def generate(
        self,
        prompt: str,
        mode: str = "coding",
        attachments: list[dict[str, Any]] | None = None,
        visual_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        has_visual = bool(visual_items)
        route = self.route_for(mode, has_visual=has_visual)
        choice = self.choose(mode, has_visual=has_visual)
        if self.settings.llm_mode != "providers" or not route:
            return self._mock_response(prompt, mode, choice, has_visual=has_visual)

        attempts = []
        for provider in route:
            current = ProviderChoice(provider, self.model_for(provider), self.reason_for(provider, mode))
            try:
                if provider == "groq":
                    result = await self._groq(prompt, current, mode)
                elif provider == "gemini":
                    result = await self._gemini(prompt, current, mode, visual_items=visual_items)
                elif provider == "cerebras":
                    result = await self._cerebras(prompt, current, mode)
                elif provider == "openrouter":
                    result = await self._openrouter(prompt, current, mode)
                elif provider == "openai":
                    result = await self._openai(prompt, current, mode)
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

        offline = self._mock_response(prompt, mode, ProviderChoice("mock", "local-planner", "free providers exhausted"), has_visual=has_visual)
        offline = self._apply_provider_notice(offline, "mock")
        offline["fallback_chain"] = attempts + [{"provider": "mock", "status": "ok"}]
        return offline

    def _mock_response(self, prompt: str, mode: str, choice: ProviderChoice, has_visual: bool = False) -> dict[str, Any]:
        summary = textwrap.shorten(" ".join(prompt.split()), width=260, placeholder="...")
        casual = "Conversa casual: sim" in prompt
        attachment_excerpt = self._attachment_excerpt(prompt)
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
                "<!DOCTYPE html>\n"
                "<html lang=\"pt-BR\">\n"
                "<head>\n"
                "  <meta charset=\"utf-8\" />\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />\n"
                "  <script src=\"https://cdn.tailwindcss.com\"></script>\n"
                "  <title>Kemy Preview</title>\n"
                "</head>\n"
                "<body class=\"min-h-screen bg-slate-950 text-white\">\n"
                "  <main class=\"mx-auto flex min-h-screen w-full max-w-6xl items-center px-6 py-14\">\n"
                "    <section class=\"grid w-full gap-8 rounded-[28px] border border-white/10 bg-white/5 p-8 shadow-2xl shadow-slate-950/40 backdrop-blur md:grid-cols-[1.1fr_.9fr] md:p-12\">\n"
                "      <div class=\"space-y-6\">\n"
                "        <span class=\"inline-flex rounded-full border border-cyan-400/30 bg-cyan-400/10 px-4 py-1 text-sm font-semibold text-cyan-200\">Kemy Artifacts</span>\n"
                "        <h1 class=\"max-w-2xl text-5xl font-black tracking-tight text-white\">Preview inicial para site/app com artifact estruturado</h1>\n"
                "        <p class=\"max-w-xl text-lg leading-8 text-slate-300\">A Kemy agora pode responder em XML de artifact, separar arquivos e publicar uma preview real sem mostrar o invólucro técnico para o usuário final.</p>\n"
                "      </div>\n"
                "      <div class=\"rounded-3xl border border-white/10 bg-slate-900/70 p-6 shadow-lg shadow-cyan-950/30\">\n"
                "        <div class=\"space-y-4\">\n"
                "          <div class=\"rounded-2xl bg-slate-800/80 p-4 text-sm text-slate-200\">index.html renderizado a partir do artifact</div>\n"
                "          <div class=\"grid gap-3 sm:grid-cols-2\">\n"
                "            <div class=\"rounded-2xl bg-emerald-400/10 p-4 text-emerald-200\">Layout responsivo</div>\n"
                "            <div class=\"rounded-2xl bg-sky-400/10 p-4 text-sky-200\">Tailwind via CDN</div>\n"
                "          </div>\n"
                "        </div>\n"
                "      </div>\n"
                "    </section>\n"
                "  </main>\n"
                "</body>\n"
                "</html>"
            )
            raw = (
                "<kemy_artifact title=\"Kemy Preview\">\n\n"
                "<file path=\"index.html\">\n"
                f"{html}\n"
                "</file>\n\n"
                "</kemy_artifact>"
            )
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": raw,
                "summary": "Artifact inicial para preview criado.",
                "files": [],
                "diff": raw,
                "tests": ["Abra o preview ao lado para validar o layout base."],
            }
        if mode == "documento":
            raw = (
                "# Documento executivo\n\n"
                "## Resumo\n"
                "Este material foi organizado para virar um DOCX profissional e um PDF pronto para envio.\n\n"
                "## Estrutura sugerida\n"
                "- Capa com titulo e contexto\n"
                "- Objetivos e escopo\n"
                "- Conteudo principal em secoes claras\n"
                "- Recomendacoes finais e proximos passos\n\n"
                "## Diretrizes visuais\n"
                "Use titulos fortes, listas objetivas, texto limpo e acabamento profissional."
            )
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": raw,
                "summary": "Estrutura base de documento preparada para DOCX e PDF.",
                "files": [],
                "diff": raw,
                "tests": ["Gerar DOCX e PDF e validar links de download."],
            }
        if has_visual and not attachment_excerpt:
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": (
                    "Recebi o anexo visual, mas nesta sessao a leitura de imagem depende de Gemini configurado em `LLM_MODE=providers` com `GEMINI_API_KEY` ativa. "
                    "Quando isso estiver ligado, a Kemy passa a descrever fotos, lousas, prints e PDFs visuais."
                ),
                "summary": "Leitura visual depende de Gemini configurado.",
                "files": [],
                "diff": "",
                "tests": [],
            }
        if attachment_excerpt:
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": (
                    "Extraí o conteúdo textual do anexo localmente. "
                    f"Trecho encontrado: {attachment_excerpt}"
                ),
                "summary": "Texto de anexo extraído localmente.",
                "files": [],
                "diff": "",
                "tests": [],
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
            "summary": (
                "Anexo visual recebido, mas a leitura visual depende de Gemini configurado. "
                + summary
                if has_visual
                else summary
            ),
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

    async def _gemini(
        self,
        prompt: str,
        choice: ProviderChoice,
        mode: str,
        visual_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.settings.gemini_api_key)
        system_prompt = f"{self._system_prompt('gemini', mode)}\n\n{prompt}"
        contents: list[Any] = [system_prompt]
        if visual_items:
            contents.extend(
                types.Part.from_bytes(data=item["data"], mime_type=item["mime_type"])
                for item in visual_items
                if item.get("data") and item.get("mime_type")
            )
        try:
            response = client.models.generate_content(model=self.settings.gemini_primary_model, contents=contents)
            self.gemini_state = {
                "active_model": self.settings.gemini_primary_model,
                "fallback_model": self.settings.gemini_fallback_model,
                "using_fallback": False,
                "message": f"Gemini usando {self.settings.gemini_primary_model}.",
                "last_error": "",
            }
            return {"provider": choice.name, "model": self.settings.gemini_primary_model, "raw": response.text, "files": [], "diff": response.text}
        except Exception as exc:
            error_text = str(exc).lower()
            blocked = any(marker in error_text for marker in ["not found", "permission", "quota", "unsupported", "access"])
            if not blocked:
                raise
            response = client.models.generate_content(model=self.settings.gemini_fallback_model, contents=contents)
            self.gemini_state = {
                "active_model": self.settings.gemini_fallback_model,
                "fallback_model": self.settings.gemini_fallback_model,
                "using_fallback": True,
                "message": f"{self.settings.gemini_primary_model} indisponivel para esta chave; fallback ativo em {self.settings.gemini_fallback_model}.",
                "last_error": str(exc)[:240],
            }
            notice = f"Aviso de sistema: {self.gemini_state['message']}"
            text = f"{notice}\n\n{response.text}".strip()
            return {"provider": choice.name, "model": self.settings.gemini_fallback_model, "raw": text, "files": [], "diff": text}

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

    async def _openai(self, prompt: str, choice: ProviderChoice, mode: str) -> dict[str, Any]:
        import httpx

        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": choice.model,
            "messages": [
                {"role": "system", "content": self._system_prompt("openai", mode)},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
        }
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return {"provider": choice.name, "model": choice.model, "raw": content, "files": [], "diff": content}

    def _system_prompt(self, provider: str, mode: str) -> str:
        base = (
            "Voce e um engenheiro senior de produto e coding. Responda em Markdown, nunca em JSON cru. "
            "Nao invente preset. Nao troque FastAPI por Flask, React por Vue, ou outra stack salvo se o usuario pedir. "
            "Entregue diagnostico, arquivos afetados, codigo/patch e testes."
        )
        base += (
            " Se houver imagem, foto, quadro, print, pagina escaneada ou PDF visual anexado, descreva o que ve e transcreva o texto importante antes de responder ao pedido principal."
        )
        if provider == "cerebras":
            base = "Voce e um auditor tecnico rapido e preciso. Responda em Markdown claro, sem JSON cru."
        if mode == "documento":
            return (
                f"{base} "
                "Se o pedido for de documento, entregue conteudo em Markdown estruturado com titulos, subtitulos, listas e texto pronto para montagem em DOCX e PDF. "
                "Se o usuario pedir slides, apresentacao, deck ou powerpoint, opere como designer profissional de apresentacoes PPTX. "
                "Antes de escrever os slides, pense internamente nesta ordem: tema central, objetivo, publico-alvo, tom, estilo visual, estrutura, layout, conteudo e PPTX. "
                "Nao crie apenas slides com texto; crie uma apresentacao visual, com identidade propria, adaptada ao tema, com layouts variados, hierarquia clara e aparencia de designer. "
                "A resposta deve ser SOMENTE Markdown de apresentacao separado por `---`, pronto para virar slides, sem explicacoes externas. "
                "Padrao obrigatorio: 7 a 9 slides, nesta cadencia quando fizer sentido: capa visual forte, introducao/contexto, desenvolvimento com layouts variados, sintese/proposta/analise/solucao e conclusao forte ou proximos passos. "
                "Cada slide deve ter um proposito unico. Nao faca slide que apenas lista topicos soltos. Nao use texto de aula, apostila ou resumo escolar. "
                "Para cada slide, defina internamente: titulo, mensagem principal, conteudo resumido, sugestao visual e layout recomendado; nao exponha essa ficha, transforme em Markdown final. "
                "Titulos: fortes, objetivos, especificos e sem numeracao. Capa: titulo, subtitulo e estilo adequado ao tema. "
                "Use pouco texto por slide; prefira frases curtas, cards, icones, imagens, numeros grandes, destaques, secoes visuais e comparacoes quando fizer sentido. "
                "Bullets: no maximo 3 por slide comum, ate 12 palavras cada. Use `**Rotulo curto** - insight concreto`. "
                "Metricas: cada linha deve ter rotulo, valor ou sinal e implicacao curta, por exemplo `**Cobertura** - 92% mapeado, lacunas priorizadas`. "
                "Roadmap/processo: use 4 ou 5 etapas com nomes de 1 a 3 palavras, seguidas de microacao. "
                "Comparacao: se usar tabela, limite a 3 colunas e 4 linhas. Celulas curtas. "
                "Fechamento: termine com uma decisao clara e uma acao de 30/60/90 dias. "
                "Regra anti-feiura: nao repita o titulo no corpo, nao use frases genericas, nao use bullets com mais de 12 palavras, nao use paragrafo longo, nao fale sobre Gamma/Canva no conteudo. "
                "Evite agenda generica, textos vazios, excesso de bullets, titulos vagos como `Ponto de Analise`, layouts repetidos e areas enormes em branco sem intencao visual. "
                "Nao corte textos, imagens ou elementos para fora do slide. Corrija gramatica, acentuacao e concordancia. "
                "Direcao visual esperada: nao existe preset universal; inferir linguagem pelo dominio e pela intencao do usuario. "
                "Comida deve soar gastronomica e sensorial; animais devem soar naturais e observacionais; moda deve soar editorial; saude deve soar limpa e cuidadosa; educacao deve soar didatica; tecnologia deve soar precisa; negocios devem soar consultivos. "
                "Se o usuario nao informar estilo, publico ou objetivo, escolha automaticamente o estilo mais adequado: academico, corporativo, emocional, tecnologico, educativo, comercial, minimalista, criativo ou institucional. "
                "Adapte cores, fontes, estilo visual e imagens ao tema solicitado; use paleta coerente com o assunto. "
                "Se o usuario der publico, marca, canal, produto, vibe ou restricao estetica, isso manda sobre a direcao generica. "
                "Entregue conteudo pensado para o renderizador PPTX aplicar capa, cards, metricas, timeline, tabela e fechamento sem estourar layout. "
                "Nao devolva JSON cru."
            )
        if mode != "site":
            return base
        return (
            f"{base} "
            "DIRETRIZ DE ARQUITETURA DE SOFTWARE (MODO KEMY ARTIFACTS). "
            "Se o pedido for de site, app, landing page, dashboard ou interface visual, responda APENAS com um bloco `<kemy_artifact title=\"...\">`. "
            "Dentro dele, cada arquivo deve ficar dentro de `<file path=\"...\">...</file>`. "
            "Nunca escreva texto fora dessas tags. Nunca use comentarios de codigo incompleto. "
            "Use Tailwind CSS via CDN quando for HTML puro. A estetica deve ser minimalista, com paletas limpas, sombras suaves e bordas arredondadas. "
            "O layout deve ser responsivo e pronto para preview imediato."
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

    def _attachment_excerpt(self, prompt: str) -> str:
        match = textwrap.dedent(prompt).split("[ANEXOS PROCESSADOS]", 1)
        if len(match) < 2:
            return ""
        if "Texto extraido do anexo:" not in match[1]:
            return ""
        extracted = " ".join(match[1].split())
        extracted = extracted.replace("Texto extraido do anexo:", "").strip()
        return textwrap.shorten(extracted, width=280, placeholder="...")
