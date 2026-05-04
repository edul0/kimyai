from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_AGENTS = [
    {
        "slug": "gabriel_cto",
        "nome": "Gabriel Junior",
        "cargo": "Gerente Geral / CTO",
        "motor_preferido": "gemini",
        "objetivo": "Quebrar pedidos em plano executavel e coordenar especialistas.",
    },
    {
        "slug": "brenno_prompt",
        "nome": "Brenno Prado",
        "cargo": "Engenheiro de Prompt Senior",
        "motor_preferido": "gemini",
        "objetivo": "Transformar ideias vagas em requisitos tecnicos claros.",
    },
    {
        "slug": "diego_cloud",
        "nome": "Diego Rodrigues",
        "cargo": "Arquiteto de TI e Cloud",
        "motor_preferido": "groq",
        "objetivo": "Definir arquitetura, APIs, banco e deploy cloud-free.",
    },
    {
        "slug": "paulo_front",
        "nome": "Paulo Lima",
        "cargo": "Desenvolvedor Front-end",
        "motor_preferido": "groq",
        "objetivo": "Gerar interfaces usaveis, responsivas e prontas para deploy.",
    },
    {
        "slug": "felipe_backend",
        "nome": "Felipe Lima",
        "cargo": "Engenheiro Backend",
        "motor_preferido": "groq",
        "objetivo": "Criar APIs, persistencia, validacao e testes.",
    },
    {
        "slug": "bianca_cyber",
        "nome": "Bianca Lima",
        "cargo": "Cyberseguranca",
        "motor_preferido": "cerebras",
        "objetivo": "Auditar codigo contra OWASP e vazamento de segredos.",
    },
    {
        "slug": "leonardo_qa",
        "nome": "Leonardo Hideki",
        "cargo": "QA Final",
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


def build_coding_prompt(message: str, mode: str, history: list[dict[str, Any]] | None = None) -> str:
    recent = history[-6:] if history else []
    history_text = "\n".join(f"- Usuario: {h.get('usuario')}\n  Kemy: {h.get('resumo')}" for h in recent)
    casual = len(message.split()) <= 4 and not any(word in message.lower() for word in ["codigo", "código", "api", "erro", "site", "deploy", "app"])
    return (
        "Voce e a Kemy AI, uma assistente conversacional e agencia multi-agente cloud-free focada em coding.\n"
        "Comporte-se como chat: mantenha contexto, responda cumprimentos e perguntas simples naturalmente. "
        "So entregue codigo, arquivos ou patch quando o usuario pedir implementacao, correcao, arquitetura, codigo, auditoria ou deploy.\n"
        "Regra absoluta: responda exatamente ao pedido do usuario. Nao substitua a stack pedida por outra. "
        "Se o usuario pedir para melhorar este sistema, analise o contexto da propria Kemy abaixo e proponha patches para ela.\n"
        "Para pedido tecnico, responda em Markdown claro com diagnostico, alteracoes recomendadas, arquivos afetados, patch/codigo quando util, testes e riscos. "
        "Para conversa casual, seja breve e natural, sem inventar codigo.\n"
        "Nao retorne JSON cru para o usuario final.\n"
        "Prioridades: gratuito, rapido, nuvem, seguranca de secrets, deploy por GitHub.\n\n"
        f"Modo: {mode}\n"
        f"Conversa casual: {'sim' if casual else 'nao'}\n"
        f"Historico recente:\n{history_text or '- sem historico'}\n\n"
        f"[CONTEXTO DO PROJETO]\n{_project_context(message)}\n\n"
        f"Pedido do usuario:\n{message}\n"
    )
