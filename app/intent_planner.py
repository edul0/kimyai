from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


IMAGE_MARKERS = [
    "gere uma imagem",
    "gera uma imagem",
    "crie uma imagem",
    "criar uma imagem",
    "faca uma imagem",
    "imagem de",
    "foto de",
    "arte de",
    "logo de",
    "banner de",
]
SLIDE_MARKERS = ["slide", "slides", "deck", "ppt", "pptx", "powerpoint", "apresentacao", "apresentar em slides"]
DOCUMENT_MARKERS = [
    "docx",
    "docxs",
    "word",
    "pdf",
    "abnt",
    "documento",
    "relatorio",
    "proposta",
    "contrato",
    "apostila",
    "manual",
    "artigo",
    "tcc",
]
SITE_MARKERS = [
    "site",
    "landing page",
    "landing",
    "dashboard",
    "interface",
    "frontend",
    "pagina",
    "app web",
    "web app",
    "html",
    "tailwind",
    "saas",
    "crud",
]
PREVIEW_MARKERS = [
    "preview",
    "live preview",
    "ver o preview",
    "abrir preview",
    "mostrar preview",
    "deixa eu ver o preview",
]
REPO_MARKERS = [
    "git",
    "github",
    "repositorio",
    "repository",
    "repo",
    "branch",
    "commit",
    "pull request",
    "pr",
    "merge",
    "deploy",
]
MAINTENANCE_MARKERS = [
    "arrume",
    "arrumar",
    "corrija",
    "corrigir",
    "conserte",
    "consertar",
    "ajuste",
    "ajustar",
    "refatore",
    "refatorar",
    "debug",
    "fix",
    "melhore",
    "melhorar",
    "atualize",
    "atualizar",
]
CODE_MARKERS = [
    "codigo",
    "api",
    "backend",
    "endpoint",
    "fastapi",
    "bug",
    "erro",
    "corrija",
    "implemente",
    "refatore",
    "supabase",
    "vercel",
    "postgres",
    "render",
    "github",
    "migration",
    "schema",
    "sql",
]
DAILY_MARKERS = [
    "resuma",
    "resumo",
    "traduza",
    "traduzir",
    "organize",
    "organizar",
    "checklist",
    "roteiro",
    "agenda",
    "planejamento",
    "plano",
    "email",
    "mensagem",
    "texto",
    "explica",
    "explique",
    "ideias",
    "brainstorm",
]
FOLLOWUP_MARKERS = ["ajuste", "melhore", "refaca", "continue", "altere", "troque", "mude", "isso", "esse", "essa"]


@dataclass(frozen=True)
class ExecutionPlan:
    mode: str
    intent: str
    stack: str
    language: str
    deliverable: str
    preview_file: str | None = None
    expected_files: list[str] = field(default_factory=list)
    run_commands: list[str] = field(default_factory=list)
    quality_rules: list[str] = field(default_factory=list)
    response_contract: str = "Responder em markdown claro."
    document_kind: str | None = None

    def as_prompt(self) -> str:
        expected = ", ".join(self.expected_files) if self.expected_files else "definir conforme necessidade"
        commands = ", ".join(self.run_commands) if self.run_commands else "sem comando obrigatorio"
        preview = self.preview_file or "sem preview obrigatorio"
        rules = "\n".join(f"- {rule}" for rule in self.quality_rules) or "- Entrega objetiva e testavel."
        kind = self.document_kind or "nao se aplica"
        return (
            "## Plano de execucao Kemy\n"
            f"- Intencao detectada: {self.intent}\n"
            f"- Modo: {self.mode}\n"
            f"- Tipo de documento: {kind}\n"
            f"- Stack recomendada: {self.stack}\n"
            f"- Linguagem principal: {self.language}\n"
            f"- Entregavel: {self.deliverable}\n"
            f"- Arquivo de preview: {preview}\n"
            f"- Arquivos esperados: {expected}\n"
            f"- Comandos de execucao: {commands}\n"
            "## Contrato de resposta\n"
            f"- {self.response_contract}\n"
            "## Regras de qualidade especificas\n"
            f"{rules}\n"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "intent": self.intent,
            "stack": self.stack,
            "language": self.language,
            "deliverable": self.deliverable,
            "preview_file": self.preview_file,
            "expected_files": self.expected_files,
            "run_commands": self.run_commands,
            "quality_rules": self.quality_rules,
            "response_contract": self.response_contract,
            "document_kind": self.document_kind,
        }


def build_execution_plan(message: str, mode: str, compact_context: dict[str, Any] | None = None) -> ExecutionPlan:
    text = _normalize(message)
    effective_mode = classify_request_mode(message, mode, compact_context)

    if effective_mode == "imagem":
        return ExecutionPlan(
            mode="imagem",
            intent="geracao de imagem",
            stack="Pollinations/image API gratuita",
            language="prompt visual",
            deliverable="imagem pronta para abrir e baixar",
            response_contract="Gerar imagem contextual sem devolver bloco de codigo.",
            quality_rules=[
                "Preservar estilo, tema e restricoes visuais solicitadas pelo usuario.",
                "Nao responder apenas com explicacao quando a intencao for imagem.",
            ],
        )
    if is_slide_request(message):
        return _slide_plan(text)
    if is_document_request(message):
        return _docx_plan()
    if effective_mode == "site":
        return _site_plan(text)
    if effective_mode == "planejamento":
        return ExecutionPlan(
            mode="planejamento",
            intent="apoio a tarefas do dia a dia",
            stack="LLM planner + contexto salvo por sessao",
            language="markdown objetivo",
            deliverable="resposta pratica com passos claros",
            response_contract="Responder como assistente operacional, sem gerar codigo se nao foi pedido.",
            quality_rules=[
                "Ser direto, util e acionavel.",
                "Usar contexto da conversa para evitar repetir perguntas.",
            ],
        )
    if _has_any(text, CODE_MARKERS):
        return ExecutionPlan(
            mode="coding",
            intent="backend ou banco de dados",
            stack="FastAPI + Supabase/Postgres quando aplicavel",
            language="python/sql",
            deliverable="patch ou arquivos backend testaveis",
            expected_files=["app/*.py", "supabase/migrations/*.sql"],
            run_commands=["python -m compileall app"],
            response_contract="Responder em markdown tecnico com diagnostico, correcao e validacao.",
            quality_rules=[
                "Preservar compatibilidade com Render.",
                "Nunca expor service_role_key no frontend.",
                "Salvar dados por usuario e manter RLS quando a tabela for exposta.",
            ],
        )
    return ExecutionPlan(
        mode=effective_mode,
        intent="codigo ou melhoria tecnica",
        stack="stack atual do repositorio",
        language="linguagem do arquivo afetado",
        deliverable="codigo corrigido, testado e pronto para GitHub/Render",
        run_commands=["python -m compileall app"],
        response_contract="Entregar resposta orientada ao resultado, sem JSON cru.",
        quality_rules=[
            "Escolher a linguagem pelo problema, nao por preset.",
            "Se for automacao simples, preferir Python; se for UI, React/TypeScript; se for banco, SQL.",
            "Entregar arquivos reais e testes possiveis, nao apenas explicacao.",
        ],
    )


def classify_request_mode(message: str, current_mode: str = "coding", session_data: dict[str, Any] | None = None) -> str:
    text = _normalize(message)
    explicit = _explicit_mode_from_text(text)
    if explicit:
        return explicit
    if _has_any(text, PREVIEW_MARKERS):
        previous = _previous_mode(session_data)
        if previous in {"site", "coding"}:
            return previous
        return "site"
    linked_repo = str((session_data or {}).get("last_github_repo") or "").strip()
    repo_bound_maintenance = bool(linked_repo) and _has_any(text, MAINTENANCE_MARKERS) and (
        _has_any(text, CODE_MARKERS + SITE_MARKERS) or len(text.split()) <= 20
    )
    if repo_bound_maintenance:
        return "coding"
    if _is_followup(text, session_data):
        previous = _previous_mode(session_data)
        if previous:
            return previous
    if current_mode in {"imagem", "site", "auditoria", "planejamento"}:
        return current_mode
    if current_mode == "documento":
        return "documento"
    if _is_daily_request(text):
        return "planejamento"
    if not _has_any(text, CODE_MARKERS + SITE_MARKERS + DOCUMENT_MARKERS + SLIDE_MARKERS + IMAGE_MARKERS):
        return "planejamento"
    return "coding"


def is_slide_request(message: str) -> bool:
    text = _normalize(message)
    return _has_any(text, SLIDE_MARKERS)


def is_document_request(message: str) -> bool:
    text = _normalize(message)
    return _has_any(text, DOCUMENT_MARKERS) and not is_slide_request(message)


def _docx_plan() -> ExecutionPlan:
    return ExecutionPlan(
        mode="documento",
        intent="documento executivo",
        stack="python-docx + reportlab/gotenberg quando configurado",
        language="markdown estruturado",
        deliverable="DOCX e PDF com titulo derivado do pedido do usuario",
        expected_files=["documento.docx", "documento.pdf"],
        response_contract="Responder com conteudo textual estruturado para documento, sem codigo-fonte.",
        quality_rules=[
            "Remover metadados internos do corpo do DOCX/PDF.",
            "Gerar titulo natural a partir do tema pesquisado, nao a frase crua do pedido.",
            "Usar secoes curtas, listas limpas e linguagem pronta para leitura.",
        ],
        document_kind="docx_pdf",
    )


def _explicit_mode_from_text(text: str) -> str | None:
    if _has_any(text, PREVIEW_MARKERS):
        return "site"
    if _has_any(text, IMAGE_MARKERS):
        return "imagem"
    if _has_any(text, SLIDE_MARKERS):
        return "documento"
    if _has_any(text, DOCUMENT_MARKERS):
        return "documento"
    repo_maintenance = _has_any(text, REPO_MARKERS) and _has_any(text, MAINTENANCE_MARKERS)
    mixed_site_code_fix = _has_any(text, SITE_MARKERS) and _has_any(text, CODE_MARKERS) and _has_any(text, MAINTENANCE_MARKERS)
    if repo_maintenance or mixed_site_code_fix:
        return "coding"
    if _has_any(text, SITE_MARKERS):
        return "site"
    if _has_any(text, CODE_MARKERS):
        return "coding"
    if _is_daily_request(text):
        return "planejamento"
    return None


def _is_daily_request(text: str) -> bool:
    if _has_any(text, CODE_MARKERS + SITE_MARKERS + DOCUMENT_MARKERS + SLIDE_MARKERS + IMAGE_MARKERS):
        return False
    if _has_any(text, DAILY_MARKERS):
        return True
    short_question = text.endswith("?") and len(text.split()) <= 18
    return short_question


def _is_followup(text: str, session_data: dict[str, Any] | None) -> bool:
    if not session_data:
        return False
    return len(text.split()) <= 14 or any(marker in text for marker in FOLLOWUP_MARKERS)


def _previous_mode(session_data: dict[str, Any] | None) -> str | None:
    stored_mode = str((session_data or {}).get("last_mode") or "").strip().lower()
    if stored_mode in {"site", "documento", "imagem", "coding", "planejamento"}:
        return stored_mode
    for item in reversed((session_data or {}).get("historico", [])[-12:]):
        mode_hint = str(item.get("mode") or (item.get("result") or {}).get("mode") or "").strip().lower()
        if mode_hint in {"site", "documento", "imagem", "coding", "planejamento"}:
            return mode_hint
        result = item.get("result") or {}
        files = item.get("files") or result.get("files") or []
        if result.get("slide_deck") or any(str(file.get("name", "")).endswith((".pptx", ".slides.html")) for file in files):
            return "documento"
        if result.get("preview_url") or result.get("site_url") or any(str(file.get("name", "")).endswith((".html", ".zip")) for file in files):
            return "site"
        if result.get("document_title") or any(str(file.get("name", "")).endswith((".docx", ".pdf")) for file in files):
            return "documento"
        if item.get("role") == "assistant" and not files:
            content = _normalize(str(item.get("content", "")))
            if _is_daily_request(content):
                return "planejamento"
    return None


def _site_plan(text: str) -> ExecutionPlan:
    simple = _has_any(text, ["html simples", "apenas html", "one page", "pagina estatica", "site simples"])
    if simple:
        return ExecutionPlan(
            mode="site",
            intent="site estatico",
            stack="HTML + CSS + JavaScript puro",
            language="html/css/javascript",
            deliverable="site self-contained com preview imediato",
            preview_file="preview.html",
            expected_files=["preview.html", "README.md"],
            run_commands=["abrir preview.html"],
            response_contract="Responder apenas com kemy_artifact contendo preview.html e README.md.",
            quality_rules=[
                "O preview deve funcionar sozinho no iframe da Kemy.",
                "Design responsivo, autoral e coerente com o tema.",
                "Nunca responder apenas codigo solto fora de kemy_artifact.",
            ],
        )
    return ExecutionPlan(
        mode="site",
        intent="site/app gerado",
        stack="React + Vite + TypeScript + Tailwind CSS",
        language="typescript/react",
        deliverable="projeto completo com preview, arquivos e ZIP para download",
        preview_file="preview.html",
        expected_files=["package.json", "index.html", "src/main.tsx", "src/App.tsx", "src/styles.css", "preview.html", "README.md"],
        run_commands=["npm install", "npm run dev", "abrir http://localhost:5173"],
        response_contract="Responder apenas com kemy_artifact contendo arquivos do projeto e preview.html valido.",
        quality_rules=[
            "Gerar preview.html self-contained alem do projeto Vite.",
            "Abrir preview automaticamente na Kemy quando o artifact tiver HTML.",
            "Usar dados mockados/localStorage se nao houver backend pronto.",
            "Incluir README com comandos e descricao das funcionalidades.",
            "Visual de produto premium: hierarquia forte, responsivo, cards, estados e microinteracoes.",
        ],
    )


def _slide_plan(text: str) -> ExecutionPlan:
    wants_pdf = "pdf" in text
    expected_files = ["apresentacao.pptx", "slides.html"]
    if wants_pdf:
        expected_files.append("apresentacao.pdf")
    return ExecutionPlan(
        mode="documento",
        intent="apresentacao/slides",
        stack="python-pptx + html deck + pdf opcional",
        language="markdown de slides + html/css",
        deliverable="slides visuais, completos e personalizados ao tema",
        preview_file="slides.html",
        expected_files=expected_files,
        response_contract="Gerar conteudo para slides; nunca devolver codigo de implementacao de docx.",
        quality_rules=[
            "Criar titulo normal a partir do tema, nao repetir a frase do pedido.",
            "Cada slide precisa de mensagem unica, pouco texto e layout variado.",
            "Adaptar estilo ao pedido: corporativo, educativo, editorial, criativo ou tecnico.",
            "Permitir imagens contextuais com boa relacao ao tema.",
            "Evitar cortes: textos devem caber no slide e o PDF deve manter proporcao 16:9.",
        ],
        document_kind="slides",
    )


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.strip().lower())


def _has_any(text: str, markers: list[str]) -> bool:
    return any(marker in text for marker in markers)
