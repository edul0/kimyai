from __future__ import annotations

from pathlib import Path
from typing import Any

from .context_memory import context_to_prompt


DEFAULT_AGENTS = [
    {
        "slug": "gabriel_cto",
        "nome": "Kimi Core",
        "cargo": "Orquestrador de Execucao",
        "motor_preferido": "gemini",
        "objetivo": "Quebrar pedidos em plano executavel e coordenar especialistas.",
    },
    {
        "slug": "brenno_prompt",
        "nome": "Kimi Spec",
        "cargo": "Analista de Requisitos",
        "motor_preferido": "gemini",
        "objetivo": "Transformar ideias vagas em requisitos tecnicos claros.",
    },
    {
        "slug": "diego_cloud",
        "nome": "Kimi Arquiteto",
        "cargo": "Arquiteto de Software e Cloud",
        "motor_preferido": "groq",
        "objetivo": "Definir arquitetura, APIs, banco e deploy cloud-free.",
    },
    {
        "slug": "paulo_front",
        "nome": "Kimi Frontend",
        "cargo": "Especialista em Interface",
        "motor_preferido": "groq",
        "objetivo": "Gerar interfaces usaveis, responsivas e prontas para deploy.",
    },
    {
        "slug": "felipe_backend",
        "nome": "Kimi Backend",
        "cargo": "Especialista em APIs e Persistencia",
        "motor_preferido": "groq",
        "objetivo": "Criar APIs, persistencia, validacao e testes.",
    },
    {
        "slug": "bianca_cyber",
        "nome": "Kimi Security",
        "cargo": "Auditor de Seguranca",
        "motor_preferido": "cerebras",
        "objetivo": "Auditar codigo contra OWASP e vazamento de segredos.",
    },
    {
        "slug": "leonardo_qa",
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
        f"Historico recente:\n{history_text or '- sem historico'}\n\n"
        f"[CONTEXTO DO PROJETO]\n{_project_context(message)}\n\n"
        f"Pedido do usuario:\n{message}\n"
    )
