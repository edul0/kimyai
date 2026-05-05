from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from .agents import build_coding_prompt
from .attachment_service import prepare_attachments
from .config import Settings
from .context_memory import build_context_snapshot, split_request_parts
from .document_service import DocumentService
from .llm_router import LLMRouter
from .pollinations import PollinationsImageService
from .schemas import JobState
from .storage import Storage
from .supabase_store import SupabaseStore
from .tools import ExternalTools


def utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


class JobManager:
    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings
        self.router = LLMRouter(settings)
        self.pollinations = PollinationsImageService(settings)
        self.tools = ExternalTools(settings)
        self.supabase = SupabaseStore(settings)
        self.documents = DocumentService(settings)

    def create(self, session_id: str, message: str, mode: str, attachments: list[dict[str, Any]] | None = None) -> JobState:
        now = utcnow()
        normalized = attachments or []
        pedido = message.strip()[: self.settings.max_prompt_chars]
        effective_mode = self._resolve_mode(pedido, mode)
        job = JobState(
            job_id=str(uuid.uuid4()),
            session_id=session_id,
            status="queued",
            etapa="Recebido na fila cloud",
            progresso=5,
            pedido=pedido,
            modo=effective_mode,
            anexos=normalized,
            created_at=now,
            updated_at=now,
        )
        self.save(job)
        return job

    def save(self, job: JobState) -> None:
        self.storage.set_json(f"job:{job.job_id}", job.model_dump(), ttl=self.settings.job_ttl_seconds)

    def get(self, job_id: str) -> JobState | None:
        data = self.storage.get_json(f"job:{job_id}")
        return JobState(**data) if data else None

    def list_recent(self) -> list[dict[str, Any]]:
        jobs = []
        for key in self.storage.keys("job:"):
            data = self.storage.get_json(key)
            if data:
                jobs.append(data)
        return sorted(jobs, key=lambda item: item.get("updated_at", ""), reverse=True)[:30]

    async def run(self, job_id: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        try:
            effective_mode = self._resolve_mode(job.pedido, job.modo)
            if effective_mode != job.modo:
                job.modo = effective_mode
                self.save(job)
            self._event(job, "Kemy", "Lendo a conversa e o contexto.", 15)
            await asyncio.sleep(0)
            session_data = self.storage.get_json(f"session:{job.session_id}", {})
            history = session_data.get("historico", [])
            memory = session_data.get("memoria", [])
            compact_context = build_context_snapshot(session_data.get("contexto_compacto"), job.pedido)
            attachment_context = prepare_attachments([item.model_dump() if hasattr(item, "model_dump") else item for item in (job.anexos or [])])

            self._event(job, "Kemy", "Preparando resposta adequada ao pedido.", 30)
            prompt = build_coding_prompt(job.pedido, job.modo, history, memory, compact_context)
            if attachment_context.get("prompt_context"):
                prompt = f"{prompt}\n\n[ANEXOS PROCESSADOS]\n{attachment_context['prompt_context']}"

            if job.modo == "imagem":
                self._event(job, "Kemy", "Pedido visual detectado. Vou gerar a imagem na rota apropriada.", 48)
                result = await self.pollinations.generate(job.pedido)
                result["tools_used"] = ["pollinations"]
                await self._finish_job(job, result)
                return

            if job.modo == "documento":
                self._event(job, "Kemy", "Pedido de documento detectado. Vou estruturar a entrega em DOCX e PDF.", 45)
                draft = await self.router.generate(
                    prompt,
                    job.modo,
                    attachments=attachment_context["items"],
                    visual_items=attachment_context["visual_items"],
                )
                self._event(job, "Kemy", "Montando arquivos finais do documento.", 72)
                result = self.documents.generate(job.session_id, job.job_id, job.pedido, draft)
                result["tools_used"] = list(
                    dict.fromkeys((draft.get("tools_used") or []) + (result.get("tools_used") or []) + ["python-docx", "reportlab"])
                )
                await self._finish_job(job, result)
                return

            self._event(job, "Kemy", "Consultando ferramentas quando necessario.", 42)
            tool_context = await self.tools.enrich(job.pedido, job.modo)
            if tool_context.get("context"):
                prompt = f"{prompt}\n\n[CONTEXTO DE FERRAMENTAS]\n{tool_context['context']}"

            self._event(job, "Kemy", "Selecionando melhor motor gratuito.", 55)
            result = await self.router.generate(
                prompt,
                job.modo,
                attachments=attachment_context["items"],
                visual_items=attachment_context["visual_items"],
            )
            result["tools_used"] = tool_context.get("used", [])
            if attachment_context["items"]:
                result["attachments_used"] = [
                    {"name": item["name"], "mime_type": item["mime_type"], "visual": item["visual"]}
                    for item in attachment_context["items"]
                ]

            self._event(job, "Kemy", "Revisando resposta antes de entregar.", 75)
            result.setdefault("security_report", "Nenhum segredo deve ser escrito no repositorio; use variaveis de ambiente.")

            await self._finish_job(job, result)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            job.status = "error"
            job.etapa = "Erro na execucao"
            job.erro = str(exc)
            job.updated_at = utcnow()
            self.save(job)
            await self.supabase.insert_job(job.model_dump())

    def _event(self, job: JobState, agente: str, msg: str, progresso: int) -> None:
        job.status = "running"
        job.etapa = msg
        job.progresso = progresso
        job.updated_at = utcnow()
        job.eventos.append({"ts": job.updated_at, "agente": agente, "msg": msg, "progresso": progresso})
        self.save(job)

    def _append_history(self, session_id: str, pedido: str, result: dict[str, Any]) -> dict[str, Any]:
        key = f"session:{session_id}"
        data = self.storage.get_json(key, {"session_id": session_id, "historico": [], "created_at": utcnow()})
        answer = result.get("raw") or result.get("summary", "")
        now = utcnow()
        history = data.setdefault("historico", [])
        previous_context = data.get("contexto_compacto") or {}
        user_context = build_context_snapshot(previous_context, pedido)
        assistant_context = build_context_snapshot(user_context, pedido, result)
        request_parts = split_request_parts(pedido)
        if not data.get("title") or data.get("title") == "Nova conversa":
            data["title"] = " ".join(pedido.split())[:58] or "Nova conversa"
        history.append(
            {
                "ts": now,
                "role": "user",
                "content": pedido,
                "request_parts": request_parts,
                "context_snapshot": user_context,
            }
        )
        history.append(
            {
                "ts": now,
                "role": "assistant",
                "content": answer[:8000],
                "context_snapshot": assistant_context,
                "provider": result.get("provider"),
                "model": result.get("model"),
                "tools_used": result.get("tools_used", []),
                "image_url": result.get("image_url"),
                "image_data_url": result.get("image_data_url"),
                "files": result.get("files", []),
                "preview_url": result.get("preview_url"),
                "result": {
                    "summary": result.get("summary"),
                    "image_url": result.get("image_url"),
                    "image_data_url": result.get("image_data_url"),
                    "provider": result.get("provider"),
                    "model": result.get("model"),
                    "files": result.get("files", []),
                    "document_title": result.get("document_title"),
                    "preview_url": result.get("preview_url"),
                },
            }
        )
        memory = data.setdefault("memoria", [])
        fact = self._memory_fact(pedido)
        if fact and fact not in memory:
            memory.append(fact)
            data["memoria"] = memory[-20:]
        data["contexto_compacto"] = assistant_context
        data["updated_at"] = now
        self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)
        return data

    def _memory_fact(self, pedido: str) -> str | None:
        text = " ".join(pedido.split())
        lowered = text.lower()
        durable_markers = [
            "quero",
            "preciso",
            "meu projeto",
            "kemy",
            "lembre",
            "deve",
            "tem que",
            "não pode",
            "nao pode",
        ]
        if len(text) < 18 or not any(marker in lowered for marker in durable_markers):
            return None
        return text[:220]

    def _resolve_mode(self, message: str, current_mode: str) -> str:
        if current_mode == "imagem":
            return "imagem"
        if current_mode == "documento":
            return "documento"
        if current_mode != "coding":
            return current_mode
        lowered = message.lower()
        image_markers = [
            "gere uma imagem",
            "gera uma imagem",
            "crie uma imagem",
            "criar uma imagem",
            "faça uma imagem",
            "faca uma imagem",
            "desenhe",
            "ilustre",
            "renderize",
            "imagem de",
            "foto de",
            "arte de",
            "logo de",
            "banner de",
        ]
        if any(marker in lowered for marker in image_markers):
            return "imagem"
        document_markers = [
            "gerar pdf",
            "gere pdf",
            "criar pdf",
            "crie pdf",
            "montar pdf",
            "gerar docx",
            "gere docx",
            "criar docx",
            "crie docx",
            "converter para pdf",
            "converta para pdf",
            "transformar em pdf",
            "transforme em pdf",
            "gerar arquivo",
            "gere arquivo",
            "criar documento",
            "crie documento",
            "montar documento",
            "gerar relatorio",
            "gerar relatório",
            "gerar proposta",
            "gerar contrato",
            "gerar apostila",
            "gerar manual",
        ]
        if any(marker in lowered for marker in document_markers):
            return "documento"
        return current_mode

    async def _finish_job(self, job: JobState, result: dict[str, Any]) -> None:
        self._event(job, "Kemy", "Finalizando mensagem.", 92)
        job.status = "done"
        job.etapa = "Concluido"
        job.progresso = 100
        job.resultado = result
        job.updated_at = utcnow()
        self.save(job)
        await self.supabase.insert_job(job.model_dump())
        session_data = self._append_history(job.session_id, job.pedido, result)
        await self.supabase.insert_session(
            job.session_id,
            owner_email=session_data.get("owner"),
            title=session_data.get("title") or "Nova sessao",
            created_at=session_data.get("created_at"),
            updated_at=session_data.get("updated_at"),
        )
        history = session_data.get("historico", [])
        user_message = history[-2] if len(history) >= 2 else {}
        assistant_message = history[-1] if history else {}
        await self.supabase.insert_message(
            job.session_id,
            "user",
            job.pedido,
            {
                "request_parts": user_message.get("request_parts", []),
                "context_snapshot": user_message.get("context_snapshot", {}),
            },
        )
        assistant_metadata = dict(result)
        assistant_metadata["context_snapshot"] = assistant_message.get("context_snapshot", {})
        await self.supabase.insert_message(job.session_id, "assistant", result.get("raw") or result.get("summary", ""), assistant_metadata)
