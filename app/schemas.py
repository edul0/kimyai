from typing import Any, Literal

from pydantic import BaseModel, Field


class AttachmentInput(BaseModel):
    name: str = Field(..., min_length=1, max_length=180)
    mime_type: str = Field(default="text/plain", max_length=120)
    content: str = Field(..., min_length=1, max_length=8000000)
    kind: Literal["text", "image", "document", "binary"] = "text"


class ComandoRequest(BaseModel):
    mensagem: str = Field(..., min_length=1, max_length=12000)
    imagem_base64: str | None = None
    anexos: list[AttachmentInput] = Field(default_factory=list, max_length=6)
    session_id: str | None = None
    github_repo: str | None = None
    modo: Literal["coding", "site", "auditoria", "planejamento", "imagem", "documento"] = "coding"


class JobCreateResponse(BaseModel):
    status: str
    job_id: str
    session_id: str
    status_url: str


class SitePublishRequest(BaseModel):
    session_id: str | None = None
    slug: str | None = Field(default=None, max_length=80)
    target: Literal["internal", "vercel"] = "internal"


class SitePublishResponse(BaseModel):
    status: str
    slug: str
    target: Literal["internal", "vercel"]
    preview_url: str
    live_url: str
    subdomain_url: str | None = None
    file_count: int = 0
    notes: str | None = None


class NovoAgente(BaseModel):
    nome: str = Field(..., min_length=2, max_length=80)
    cargo: str = Field(..., min_length=2, max_length=120)
    objetivo: str = Field(..., min_length=8, max_length=2000)
    backstory: str | None = Field(default=None, max_length=4000)
    motor: str = "groq"
    pasta_rag: str | None = None


class JobState(BaseModel):
    job_id: str
    session_id: str
    status: Literal["queued", "running", "done", "error"]
    etapa: str
    progresso: int = Field(ge=0, le=100)
    pedido: str
    modo: str = "coding"
    created_at: str
    updated_at: str
    resultado: dict[str, Any] | None = None
    erro: str | None = None
    eventos: list[dict[str, Any]] = Field(default_factory=list)
    anexos: list[AttachmentInput] = Field(default_factory=list)
    github_repo: str | None = None
