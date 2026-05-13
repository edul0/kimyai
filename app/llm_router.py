from __future__ import annotations

import html
import re
import textwrap
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from .config import Settings


@dataclass(frozen=True)
class ProviderChoice:
    name: str
    model: str
    reason: str


@dataclass
class ProviderMetrics:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    avg_latency_ms: float = 0.0
    last_error: str = ""
    last_success_at: float = 0.0


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
        self.provider_metrics: dict[str, dict[str, ProviderMetrics]] = {}
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
        if self.settings.adaptive_router_enabled and available:
            available = sorted(
                available,
                key=lambda provider: self._provider_score(mode, provider, route.index(provider)),
                reverse=True,
            )
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
            started = time.perf_counter()
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
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_success(mode, provider, latency_ms)
                result = self._apply_provider_notice(result, provider)
                result["fallback_chain"] = attempts + [{"provider": provider, "status": "ok"}]
                result["route_debug"] = self.route_debug(mode)
                return result
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                self._record_failure(mode, provider, latency_ms, exc)
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
        offline["route_debug"] = self.route_debug(mode)
        return offline

    def route_debug(self, mode: str) -> dict[str, Any]:
        route = self.ROUTES.get(mode, self.ROUTES["coding"])
        stats: list[dict[str, Any]] = []
        for provider in route:
            metrics = self._metrics(mode, provider)
            stats.append(
                {
                    "provider": provider,
                    "attempts": metrics.attempts,
                    "successes": metrics.successes,
                    "failures": metrics.failures,
                    "avg_latency_ms": round(metrics.avg_latency_ms, 1),
                    "score": round(self._provider_score(mode, provider, route.index(provider)), 4),
                    "last_error": metrics.last_error,
                }
            )
        return {"mode": mode, "adaptive_enabled": self.settings.adaptive_router_enabled, "providers": stats}

    def _metrics(self, mode: str, provider: str) -> ProviderMetrics:
        mode_stats = self.provider_metrics.setdefault(mode, {})
        return mode_stats.setdefault(provider, ProviderMetrics())

    def _record_success(self, mode: str, provider: str, latency_ms: float) -> None:
        metrics = self._metrics(mode, provider)
        metrics.attempts += 1
        metrics.successes += 1
        metrics.last_success_at = time.time()
        metrics.last_error = ""
        if metrics.successes == 1:
            metrics.avg_latency_ms = latency_ms
            return
        weight = 0.22
        metrics.avg_latency_ms = (1 - weight) * metrics.avg_latency_ms + weight * latency_ms

    def _record_failure(self, mode: str, provider: str, latency_ms: float, exc: Exception) -> None:
        metrics = self._metrics(mode, provider)
        metrics.attempts += 1
        metrics.failures += 1
        metrics.last_error = str(exc)[:240]
        if metrics.avg_latency_ms <= 0:
            metrics.avg_latency_ms = latency_ms
            return
        weight = 0.22
        metrics.avg_latency_ms = (1 - weight) * metrics.avg_latency_ms + weight * latency_ms

    def _provider_score(self, mode: str, provider: str, base_index: int) -> float:
        metrics = self._metrics(mode, provider)
        if metrics.attempts == 0:
            return 0.5 - base_index * 0.001
        success_rate = metrics.successes / max(metrics.attempts, 1)
        latency_penalty = min(metrics.avg_latency_ms / 3000.0, 1.2)
        failure_penalty = min(metrics.failures / max(metrics.attempts, 1), 1.0)
        recency_bonus = 0.08 if metrics.last_success_at and (time.time() - metrics.last_success_at) < 900 else 0.0
        stability_bonus = 0.05 if metrics.failures == 0 and metrics.successes >= 3 else 0.0
        return (success_rate * 1.45) - (latency_penalty * 0.25) - (failure_penalty * 0.7) + recency_bonus + stability_bonus

    def _mock_response(self, prompt: str, mode: str, choice: ProviderChoice, has_visual: bool = False) -> dict[str, Any]:
        summary = textwrap.shorten(" ".join(prompt.split()), width=260, placeholder="...")
        casual = "Conversa casual: sim" in prompt
        attachment_excerpt = self._attachment_excerpt(prompt)
        attachment_context = self._attachment_context(prompt)
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
            raw, site_summary = self._build_mock_site_artifact(prompt, attachment_context)
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": raw,
                "summary": site_summary,
                "files": [],
                "diff": raw,
                "tests": [
                    "Abra o live preview para validar o visual.",
                    "Peça ajustes no campo abaixo do preview para evoluir o mesmo projeto.",
                ],
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
        if has_visual and not attachment_excerpt and not attachment_context:
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
        if attachment_context and has_visual:
            return {
                "provider": choice.name,
                "model": choice.model,
                "reason": choice.reason,
                "raw": (
                    "Analisei o anexo visual com leitura local de composicao (paleta, brilho e estilo). "
                    "Resumo capturado:\n\n"
                    f"{attachment_context}"
                ),
                "summary": "Referencia visual local analisada.",
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
                "Anexo visual processado localmente com resumo de estilo. " + summary
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
                "Se o pedido mencionar docx, word, abnt, relatorio ou pdf e NAO mencionar slide/deck/ppt, nunca retorne codigo Python, classes, funcoes ou snippets tecnicos. "
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
        if mode == "coding":
            return (
                f"{base} "
                "Modo coding estrito: se o pedido for para corrigir, estruturar, refatorar ou evoluir codigo existente, nao mude para criacao de site novo e nao troque o tipo de entrega. "
                "Use o contexto do workspace/repositorio ativo e proponha alteracoes diretamente nos arquivos desse projeto. "
                "Quando o usuario mencionar Vercel, Supabase, Render, GitHub ou deploy, entregue passos praticos e configuracoes reais dessa integracao no proprio projeto. "
                "Nao invente funcoes fora do pedido. Se houver ambiguidade, escolha a interpretacao mais conservadora focada no objetivo principal do usuario."
            )
        if mode != "site":
            return base
        return (
            f"{base} "
            "DIRETRIZ DE ARQUITETURA DE SOFTWARE (MODO KEMY ARTIFACTS). "
            "Voce e uma IA programadora full-stack senior e designer de produto. Transforme qualquer pedido de site, app, dashboard, landing page, SaaS, painel ou CRUD em um projeto completo, bonito, responsivo, organizado e pronto para rodar localmente sem servicos pagos. "
            "Antes de codar, pense internamente: objetivo do sistema, publico-alvo, telas necessarias, funcionalidades principais, estilo visual e dados simulados. "
            "Se houver imagem de referencia anexada, trate como direcao de arte obrigatoria: leia composicao, paleta, contraste, atmosfera, tipografia percebida e hierarquia visual. "
            "Nao ignore imagens anexadas quando o usuario pedir 'baseie nisso' ou 'nesse estilo'. "
            "Se o pedido for de site, app, landing page, dashboard ou interface visual, responda APENAS com um bloco `<kemy_artifact title=\"...\">`. "
            "Dentro dele, cada arquivo deve ficar dentro de `<file path=\"...\">...</file>`. "
            "Nunca escreva texto fora dessas tags. Nunca entregue codigo incompleto, comentarios como `adicione aqui`, imports quebrados ou arquivos faltando. "
            "Tecnologia padrao para app/sistema/dashboard/SaaS/CRUD: React + Vite + TypeScript + Tailwind CSS, Lucide React para icones, Recharts para graficos quando fizer sentido, LocalStorage e dados simulados quando nao houver backend. "
            "Se o projeto for simples, pode usar HTML/CSS/JS puro, mas ainda deve parecer produto real. "
            "Sempre inclua `package.json`, `index.html`, `src/main.tsx`, `src/App.tsx`, `src/styles.css` ou equivalentes quando usar Vite. "
            "Sempre inclua tambem um `preview.html` self-contained quando possivel, com CSS/JS inline ou CDN gratuita, para o sistema da Kemy exibir preview imediato em iframe sem rodar npm no servidor. "
            "A interface deve ter qualidade visual real: layout limpo, cards espacados, tipografia forte, cores coerentes, botoes com hover, icones, microinteracoes, responsividade desktop/mobile, boa hierarquia e nada de tela branca crua. "
            "Use liberdade criativa com responsabilidade: hero forte, planos de fundo com imagem contextual (quando fizer sentido), overlays elegantes, gradientes com profundidade, seções narrativas e componentes com personalidade. "
            "Evite design sem alma: cartao branco basico, texto generico e layout de template vazio."
            "Adapte estetica ao tema: financeiro confiavel, RPG imersivo, educacao clara, saude calma, SaaS premium, portfolio autoral, agro verde/terra/tecnologia rural. "
            "Sempre que fizer sentido implemente navbar ou sidebar, dashboard inicial, cards de estatisticas, tabelas, filtros, busca, modal, formularios, validacao basica, CRUD local, graficos e pagina de detalhes. "
            "Revise mentalmente antes de responder: imports existem, componentes fecham, Tailwind esta correto, layout esta bonito, roda sem pagar nada e ha comandos claros. "
            "Dentro do artifact, inclua um arquivo `README.md` com: resumo, estrutura, `npm install`, `npm run dev`, URL `http://localhost:5173` e como testar funcionalidades. "
            "Regra final: se a interface parecer crua, simples demais, desalinhada, sem espacamento, sem identidade visual ou HTML basico, refaca o layout antes de entregar."
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

    def _extract_user_request(self, prompt: str) -> str:
        marker = "Pedido do usuario:"
        if marker in prompt:
            tail = prompt.split(marker, 1)[1]
            cleaned = " ".join(tail.split())
            return textwrap.shorten(cleaned, width=260, placeholder="...")
        cleaned = " ".join(prompt.split())
        return textwrap.shorten(cleaned, width=220, placeholder="...")

    def _attachment_context(self, prompt: str, width: int = 1300) -> str:
        match = textwrap.dedent(prompt).split("[ANEXOS PROCESSADOS]", 1)
        if len(match) < 2:
            return ""
        section = match[1]
        for stopper in ["[CONTEXTO DE FERRAMENTAS]", "[SITE ATUAL - EDICAO INCREMENTAL]", "[CONTEXTO DO PROJETO]", "Pedido do usuario:"]:
            if stopper in section:
                section = section.split(stopper, 1)[0]
        normalized = " ".join(section.split()).strip()
        if not normalized:
            return ""
        return textwrap.shorten(normalized, width=width, placeholder="...")

    def _mock_site_title(self, request: str) -> str:
        lowered = request.lower()
        if any(word in lowered for word in ["barbear", "barber", "cabelo", "corte"]):
            return "Barbearia Premium"
        if "clinica" in lowered or "saude" in lowered:
            return "Clinica Essencial"
        if "restaurante" in lowered or "menu" in lowered:
            return "Reserva Gourmet"
        if "imobili" in lowered:
            return "Imobiliaria Prime"
        cleaned = re.sub(r"[^0-9A-Za-zÀ-ÿ ]+", " ", request).strip()
        if not cleaned:
            return "Site Kemy"
        words = cleaned.split()
        return " ".join(words[:5]).title()

    def _mock_site_palette(self, request: str, attachment_context: str) -> dict[str, str]:
        lowered = f"{request} {attachment_context}".lower()
        dark = any(marker in lowered for marker in ["fundo escuro", "escura", "noturna", "luxo", "premium", "cinemat"])
        barber = any(marker in lowered for marker in ["barbear", "barber", "cabelo", "corte"])
        if barber:
            return {
                "bg": "#05080f" if dark else "#0b1120",
                "bg2": "#0f172a",
                "accent": "#d7b67a",
                "accent_soft": "rgba(215, 182, 122, 0.18)",
                "text": "#f8fafc",
                "muted": "#c7d2fe",
                "button_text": "#0b1020",
            }
        if dark:
            return {
                "bg": "#051a18",
                "bg2": "#0b2139",
                "accent": "#67e8f9",
                "accent_soft": "rgba(103, 232, 249, 0.16)",
                "text": "#eff6ff",
                "muted": "#cbd5e1",
                "button_text": "#042f2e",
            }
        return {
            "bg": "#0f172a",
            "bg2": "#1d4ed8",
            "accent": "#22d3ee",
            "accent_soft": "rgba(34, 211, 238, 0.18)",
            "text": "#ecfeff",
            "muted": "#dbeafe",
            "button_text": "#082f49",
        }

    def _pollinations_image_url(self, request: str, attachment_context: str) -> str:
        query = f"{request}. {attachment_context}. editorial website hero photo, cinematic lighting, ultra detailed"
        safe = quote_plus(" ".join(query.split())[:220])
        return f"https://image.pollinations.ai/prompt/{safe}?width=1920&height=1080&nologo=true&enhance=true&safe=true"

    def _build_mock_site_artifact(self, prompt: str, attachment_context: str) -> tuple[str, str]:
        request = self._extract_user_request(prompt)
        title = self._mock_site_title(request)
        palette = self._mock_site_palette(request, attachment_context)
        image_url = self._pollinations_image_url(request, attachment_context)
        badge = "AGENDA ONLINE" if "barbear" in request.lower() else "EXPERIENCIA DIGITAL"
        hero_title = (
            "Transforme seu estilo com excelência."
            if "barbear" in request.lower()
            else f"{title} com identidade premium"
        )
        subtitle = (
            "Agende em segundos, com visual forte, prova social e experiencia de alto padrao."
            if "barbear" in request.lower()
            else "Uma interface de alto impacto com foco em conversao, confianca e fluidez."
        )
        reference = html.escape(attachment_context[:420]) if attachment_context else "Sem imagem de referencia anexada."
        request_safe = html.escape(request)
        title_safe = html.escape(title)
        badge_safe = html.escape(badge)
        hero_title_safe = html.escape(hero_title)
        subtitle_safe = html.escape(subtitle)
        html_doc = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title_safe}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@500;700;800&family=Sora:wght@600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: {palette["bg"]};
      --bg2: {palette["bg2"]};
      --accent: {palette["accent"]};
      --accent-soft: {palette["accent_soft"]};
      --text: {palette["text"]};
      --muted: {palette["muted"]};
      --button-text: {palette["button_text"]};
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Manrope", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(90% 120% at 80% -20%, color-mix(in srgb, var(--accent), transparent 72%), transparent 54%),
        linear-gradient(120deg, var(--bg), var(--bg2));
    }}
    .hero {{
      min-height: 100vh;
      position: relative;
      isolation: isolate;
      display: grid;
      place-items: center;
      padding: 28px;
      overflow: hidden;
    }}
    .hero::before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(120deg, rgba(4, 8, 20, 0.82), rgba(4, 8, 20, 0.58)),
        url("{image_url}");
      background-size: cover;
      background-position: center;
      z-index: -2;
      transform: scale(1.02);
    }}
    .hero::after {{
      content: "";
      position: absolute;
      inset: 0;
      background:
        radial-gradient(72% 90% at 12% 20%, color-mix(in srgb, var(--accent), transparent 68%), transparent 60%),
        linear-gradient(180deg, rgba(2, 6, 23, 0.08), rgba(2, 6, 23, 0.6));
      z-index: -1;
    }}
    .shell {{
      width: min(1120px, 100%);
      border: 1px solid rgba(255,255,255,0.16);
      background: linear-gradient(160deg, rgba(9, 12, 20, 0.72), rgba(9, 12, 20, 0.54));
      backdrop-filter: blur(8px);
      border-radius: 28px;
      padding: clamp(22px, 3.4vw, 40px);
      box-shadow: 0 22px 64px rgba(2, 6, 23, 0.52);
    }}
    .brand {{
      display: inline-flex;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.22);
      background: var(--accent-soft);
      color: var(--text);
      font-size: 12px;
      letter-spacing: .24em;
      text-transform: uppercase;
      font-weight: 800;
      padding: 8px 14px;
    }}
    h1 {{
      margin: 18px 0 14px;
      font-family: "Sora", sans-serif;
      font-size: clamp(2rem, 6.2vw, 4.6rem);
      line-height: .95;
      letter-spacing: -0.03em;
      max-width: 11ch;
    }}
    .subtitle {{
      margin: 0;
      max-width: 60ch;
      color: var(--muted);
      font-size: clamp(1rem, 2.2vw, 1.55rem);
      line-height: 1.45;
    }}
    .cta {{
      margin-top: 26px;
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
    }}
    .btn {{
      border: 0;
      border-radius: 14px;
      font-weight: 800;
      padding: 14px 22px;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: transform .15s ease, box-shadow .2s ease;
    }}
    .btn-primary {{
      background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent), #ffffff 20%));
      color: var(--button-text);
      box-shadow: 0 12px 32px color-mix(in srgb, var(--accent), transparent 70%);
    }}
    .btn-secondary {{
      border: 1px solid rgba(255,255,255,0.26);
      background: rgba(255,255,255,0.05);
      color: var(--text);
    }}
    .btn:hover {{ transform: translateY(-2px); }}
    .proof {{
      margin-top: 26px;
      display: grid;
      gap: 10px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .metric {{
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,0.14);
      background: rgba(255,255,255,0.06);
      padding: 12px 14px;
      font-size: 14px;
      color: var(--muted);
    }}
    .metric strong {{
      display: block;
      color: var(--text);
      font-size: 26px;
      line-height: 1.1;
      margin-bottom: 4px;
    }}
    .ref {{
      margin-top: 20px;
      border-radius: 14px;
      border: 1px dashed rgba(255,255,255,0.28);
      background: rgba(2,6,23,0.32);
      padding: 12px 14px;
      font-size: 13px;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <section class="hero">
    <article class="shell">
      <span class="brand">{badge_safe}</span>
      <h1>{hero_title_safe}</h1>
      <p class="subtitle">{subtitle_safe}</p>
      <div class="cta">
        <a class="btn btn-primary" href="#">Agendar agora</a>
        <a class="btn btn-secondary" href="#">Ver serviços</a>
      </div>
      <section class="proof">
        <div class="metric"><strong>5.0</strong>avaliação média dos clientes</div>
        <div class="metric"><strong>58+</strong>agendamentos confirmados no mês</div>
        <div class="metric"><strong>24h</strong>resposta rápida no WhatsApp</div>
      </section>
      <div class="ref"><strong>Pedido atual:</strong> {request_safe}<br><strong>Referência visual:</strong> {reference}</div>
    </article>
  </section>
</body>
</html>"""
        readme = f"""# {title}

Projeto de preview gerado no modo local da Kemy com direção visual reforçada.

## Como evoluir
- Abra `preview.html` no navegador.
- Use o campo "Edite este projeto em tempo real" no live preview para pedir ajustes.
- Exemplo: `deixe mais editorial, com tons dourados e seção de depoimentos`.

## Referência de entrada
- Pedido: {request}
- Contexto visual detectado: {attachment_context or 'sem anexo visual'}
"""
        raw = (
            f"<kemy_artifact title=\"{title_safe}\">\n"
            "<file path=\"preview.html\">\n"
            f"{html_doc}\n"
            "</file>\n"
            "<file path=\"README.md\">\n"
            f"{readme}\n"
            "</file>\n"
            "</kemy_artifact>"
        )
        summary = "Preview criativo gerado com direcao visual baseada no pedido e no anexo."
        return raw, summary

    def _attachment_excerpt(self, prompt: str) -> str:
        match = textwrap.dedent(prompt).split("[ANEXOS PROCESSADOS]", 1)
        if len(match) < 2:
            return ""
        body = match[1]
        if "Texto extraido do anexo:" not in body and "Texto extraído do anexo:" not in body:
            return ""
        extracted = " ".join(body.split())
        extracted = extracted.replace("Texto extraido do anexo:", "").replace("Texto extraído do anexo:", "").strip()
        return textwrap.shorten(extracted, width=280, placeholder="...")
