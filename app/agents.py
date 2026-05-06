from __future__ import annotations

from pathlib import Path
from typing import Any

from .context_memory import context_to_prompt


DEFAULT_AGENTS = [
    {
        "slug": "Orquestrador_cto",
        "nome": "Kimi Core",
        "cargo": "Orquestrador de Execucao",
        "motor_preferido": "gemini",
        "objetivo": "Quebrar pedidos em plano executavel e coordenar especialistas.",
    },
    {
        "slug": "Analista_prompt",
        "nome": "Kimi Spec",
        "cargo": "Analista de Requisitos",
        "motor_preferido": "gemini",
        "objetivo": "Transformar ideias vagas em requisitos tecnicos claros.",
    },
    {
        "slug": "Arquiteto_cloud",
        "nome": "Kimi Arquiteto",
        "cargo": "Arquiteto de Software e Cloud",
        "motor_preferido": "groq",
        "objetivo": "Definir arquitetura, APIs, banco e deploy cloud-free.",
    },
    {
        "slug": "Interface_front",
        "nome": "Kimi Frontend",
        "cargo": "Especialista em Interface",
        "motor_preferido": "groq",
        "objetivo": "Gerar interfaces usaveis, responsivas e prontas para deploy.",
    },
    {
        "slug": "EspApi_backend",
        "nome": "Kimi Backend",
        "cargo": "Especialista em APIs e Persistencia",
        "motor_preferido": "groq",
        "objetivo": "Criar APIs, persistencia, validacao e testes.",
    },
    {
        "slug": "Segurança_cyber",
        "nome": "Kimi Security",
        "cargo": "Auditor de Seguranca",
        "motor_preferido": "cerebras",
        "objetivo": "Auditar codigo contra OWASP e vazamento de segredos.",
    },
    {
        "slug": "Validador_qa",
        "nome": "Kimi QA",
        "cargo": "Validador Final de Entrega",
        "motor_preferido": "cerebras",
        "objetivo": "Validar entrega, testes e formato final.",
    },
]


def _project_context(message: str) -> str:
    triggers = ["proprio sistema", "próprio sistema", "seu sistema", "este sistema", "kemy", "melhore seu codigo", "melhore seu código"]
    if not any(trigger in message.lower() for trigger in triggers):
        return (
            "Arquitetura Kemy atual: FastAPI em app/main.py, jobs em app/jobs.py, roteamento LLM em app/llm_router.py, "
            "ferramentas externas em app/tools.py, UI estatica em static/, schema Supabase isolado em supabase/migrations/."
        )

    root = Path(__file__).resolve().parent.parent
    files = ["app/main.py", "app/jobs.py", "app/llm_router.py", "app/auth.py", "static/index.html", "static/app.js"]
    chunks = []
    for rel in files:
        path = root / rel
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore")
            chunks.append(f"## {rel}\n{text[:2200]}")
    return "\n\n".join(chunks)


def build_coding_prompt(
    message: str,
    mode: str,
    history: list[dict[str, Any]] | None = None,
    memory: list[str] | None = None,
    compact_context: dict[str, Any] | None = None,
    refined_prompt: str | None = None,
) -> str:
    recent = history[-6:] if history else []
    history_lines = []
    for h in recent:
        if h.get("role") and h.get("content"):
            history_lines.append(f"- {h.get('role')}: {h.get('content')}")
        else:
            history_lines.append(f"- Usuario: {h.get('usuario')}\n  Kemy: {h.get('resumo')}")
    history_text = "\n".join(history_lines)
    memory_text = "\n".join(f"- {item}" for item in (memory or [])[-10:])
    technical_words = [
        "codigo",
        "código",
        "api",
        "erro",
        "site",
        "deploy",
        "app",
        "imagem",
        "gere",
        "gerar",
        "desenhe",
        "ilustre",
        "implemente",
        "crie",
        "corrija",
        "teste",
        "banco",
        "supabase",
        "github",
        "render",
        "docx",
        "pdf",
        "documento",
        "relatorio",
        "relatório",
    ]
    casual = len(message.split()) <= 5 and not any(word in message.lower() for word in technical_words)
    return (
        "Voce e a Kemy AI, uma assistente conversacional e agencia multi-agente cloud-free focada em coding.\n"
        "Comporte-se como chat com memoria: entenda a intencao do usuario antes de agir. "
        "Se for conversa, responda conversa. Se for duvida, explique. Se for tarefa tecnica, planeje e execute mentalmente como agente de coding. "
        "So entregue codigo, arquivos ou patch quando o usuario pedir implementacao, correcao, arquitetura, codigo, auditoria ou deploy.\n"
        "Classifique internamente a intencao em: conversa, pergunta, coding, pesquisa, imagem, documento, voz, pc, deploy. "
        "Para imagem, quando houver API visual conectada, gere a imagem em vez de apenas explicar. "
        "Para documento, quando o usuario pedir DOCX, Word, relatorio, proposta, contrato ou PDF, pense em uma entrega organizada, bonita e pronta para exportacao. "
        "Para voz ou controle de PC, explique o que ja e possivel pela infraestrutura atual e qual ferramenta precisa ser conectada.\n"
        "Regra absoluta: responda exatamente ao pedido do usuario. Nao substitua a stack pedida por outra. "
        "Se o usuario pedir para melhorar este sistema, analise o contexto da propria Kemy abaixo e proponha patches para ela.\n"
        "Use o contexto compactado como memoria operacional. Entenda pedidos longos por partes e resolva cada parte sem perder as restricoes anteriores. "
        "A cada nova pergunta, seja mais especifica usando as preferencias, requisitos e pendencias ja aprendidas. "
        "Se faltar uma informacao que bloqueia a execucao, faca uma pergunta curta; se nao bloquear, assuma o caminho mais provavel e continue.\n"
        "Para pedido tecnico, responda em Markdown claro com diagnostico, alteracoes recomendadas, arquivos afetados, patch/codigo quando util, testes e riscos. "
        "Para conversa casual, seja breve e natural, sem inventar codigo.\n"
        "Nao retorne JSON cru para o usuario final.\n"
        "Prioridades: gratuito, rapido, nuvem, seguranca de secrets, deploy por GitHub.\n\n"
        f"Modo: {mode}\n"
        f"Conversa casual: {'sim' if casual else 'nao'}\n"
        f"Memoria duravel desta sessao:\n{memory_text or '- sem memoria duravel ainda'}\n\n"
        f"Contexto compactado da sessao:\n{context_to_prompt(compact_context)}\n\n"
        f"Brief da fazedora de prompts:\n{refined_prompt or '- nenhum brief adicional'}\n\n"
        f"Historico recente:\n{history_text or '- sem historico'}\n\n"
        f"[CONTEXTO DO PROJETO]\n{_project_context(message)}\n\n"
        f"Pedido do usuario:\n{message}\n"
    )


def build_prompt_refiner_prompt(
    message: str,
    mode: str,
    history: list[dict[str, Any]] | None = None,
    compact_context: dict[str, Any] | None = None,
) -> str:
    is_slide_request = mode == "documento" and any(
        marker in message.lower()
        for marker in ["slide", "slides", "deck", "ppt", "pptx", "powerpoint", "apresentacao"]
    )
    recent = history[-4:] if history else []
    history_text = "\n".join(
        f"- {item.get('role', 'usuario')}: {str(item.get('content') or '')[:400]}"
        for item in recent
        if item.get("content")
    )
    slide_rules = (
        "\nRegras extras para slides PPTX:\n"
        "- Transforme o pedido em uma narrativa de consultoria, nao em um resumo escolar.\n"
        "- Defina uma promessa clara para a capa: controle, risco, economia, crescimento ou decisao.\n"
        "- Exija titulos de ate 6 palavras e bullets de ate 12 palavras.\n"
        "- Proiba frases genericas como `visual executivo`, `visao geral` sem contexto, `deck personalizado` e repeticao do titulo.\n"
        "- Peça agenda com itens de 2 a 4 palavras, metricas com valor explicito e fechamento com acao de 30/60/90 dias.\n"
        "- O resultado deve sair pronto para PPTX, com texto curto, contrastes claros e ritmo de apresentacao premium.\n"
        if is_slide_request
        else ""
    )
    return (
        "Voce e Kemy Spec, a fazedora de prompts da Kemy AI.\n"
        "Sua unica funcao e transformar o pedido do usuario em um brief de execucao impecavel para o orquestrador.\n"
        "Responda em Markdown curto, objetivo e altamente acionavel.\n"
        "Estrutura obrigatoria:\n"
        "## Objetivo\n"
        "## Entregavel esperado\n"
        "## Requisitos tecnicos\n"
        "## Restricoes e preferencias\n"
        "## Criterios de qualidade\n"
        "## Proximo passo do orquestrador\n"
        "Nao escreva codigo. Nao execute nada. Nao converse com o usuario. Apenas refine o pedido.\n\n"
        f"{slide_rules}\n"
        f"Modo alvo: {mode}\n"
        f"Contexto compacto:\n{context_to_prompt(compact_context)}\n\n"
        f"Historico recente:\n{history_text or '- sem historico'}\n\n"
        f"Pedido bruto:\n{message}\n"
    )


def build_local_prompt_brief(message: str, mode: str, compact_context: dict[str, Any] | None = None) -> str:
    is_slide_request = mode == "documento" and any(
        marker in message.lower()
        for marker in ["slide", "slides", "deck", "ppt", "pptx", "powerpoint", "apresentacao"]
    )
    slide_quality = (
        "- Se for PPTX, gerar narrativa premium com capa forte, agenda curta, insights, metricas e fechamento acionavel.\n"
        "- Evitar texto generico, frases longas, repeticao de titulo e linguagem escolar.\n"
        "- Priorizar frases curtas, hierarquia clara e conteudo que caiba em cards de apresentacao.\n"
        if is_slide_request
        else ""
    )
    return (
        "## Objetivo\n"
        f"- Resolver o pedido em modo `{mode}` sem perder o foco no resultado final.\n\n"
        "## Entregavel esperado\n"
        f"- Entrega concreta baseada em: {message[:240]}\n\n"
        "## Requisitos tecnicos\n"
        "- Respeitar a stack atual do projeto.\n"
        "- Priorizar fluxo cloud-free e artefatos utilizaveis.\n\n"
        "## Restricoes e preferencias\n"
        f"- Contexto aprendido: {context_to_prompt(compact_context)}\n\n"
        "## Criterios de qualidade\n"
        "- Saida final consistente, especifica e testavel.\n\n"
        f"{slide_quality}"
        "## Proximo passo do orquestrador\n"
        "- Planejar a execucao, escolher o motor e produzir a entrega final.\n"
    )
