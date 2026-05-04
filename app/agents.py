from __future__ import annotations

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


def build_coding_prompt(message: str, mode: str, history: list[dict[str, Any]] | None = None) -> str:
    recent = history[-6:] if history else []
    history_text = "\n".join(f"- Usuario: {h.get('usuario')}\n  Kemy: {h.get('resumo')}" for h in recent)
    return (
        "Voce e a Kemy AI, uma agencia multi-agente cloud-free focada em coding.\n"
        "Responda com entregaveis de programador senior: arquivos, diffs, testes, riscos e proximos passos.\n"
        "Prioridades: gratuito, rapido, nuvem, seguranca de secrets, deploy por GitHub.\n\n"
        f"Modo: {mode}\n"
        f"Historico recente:\n{history_text or '- sem historico'}\n\n"
        f"Pedido do usuario:\n{message}\n"
    )

