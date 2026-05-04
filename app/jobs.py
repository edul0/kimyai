from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

from .agents import build_coding_prompt
from .config import Settings
from .llm_router import LLMRouter
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
        self.tools = ExternalTools(settings)
        self.supabase = SupabaseStore(settings)

    def create(self, session_id: str, message: str, mode: str, attachments: list[dict[str, Any]] | None = None) -> JobState:
        now = utcnow()
        normalized = attachments or []
        job = JobState(
            job_id=str(uuid.uuid4()),
            session_id=session_id,
            status="queued",
            etapa="Recebido na fila cloud",
            progresso=5,
            pedido=self._compose_request(message, normalized)[: self.settings.max_prompt_chars],
            modo=mode,
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
            self._event(job, "Kemy", "Lendo a conversa e o contexto.", 15)
            await asyncio.sleep(0)
            session_data = self.storage.get_json(f"session:{job.session_id}", {})
            history = session_data.get("historico", [])
            memory = session_data.get("memoria", [])

            self._event(job, "Kemy", "Preparando resposta adequada ao pedido.", 30)
            prompt = build_coding_prompt(job.pedido, job.modo, history, memory)

            self._event(job, "Kemy", "Consultando ferramentas quando necessario.", 42)
            tool_context = await self.tools.enrich(job.pedido, job.modo)
            if tool_context.get("context"):
                prompt = f"{prompt}\n\n[CONTEXTO DE FERRAMENTAS]\n{tool_context['context']}"

            self._event(job, "Kemy", "Selecionando melhor motor gratuito.", 55)
            result = await self.router.generate(prompt, job.modo)
            result["tools_used"] = tool_context.get("used", [])

            self._event(job, "Kemy", "Revisando resposta antes de entregar.", 75)
            result.setdefault("security_report", "Nenhum segredo deve ser escrito no repositorio; use variaveis de ambiente.")

            self._event(job, "Kemy", "Finalizando mensagem.", 92)
            job.status = "done"
            job.etapa = "Concluido"
            job.progresso = 100
            job.resultado = result
            job.updated_at = utcnow()
            self.save(job)
            await self.supabase.insert_job(job.model_dump())
            self._append_history(job.session_id, job.pedido, result)
            await self.supabase.insert_message(job.session_id, "user", job.pedido)
            await self.supabase.insert_message(job.session_id, "assistant", result.get("raw") or result.get("summary", ""), result)
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

    def _append_history(self, session_id: str, pedido: str, result: dict[str, Any]) -> None:
        key = f"session:{session_id}"
        data = self.storage.get_json(key, {"session_id": session_id, "historico": [], "created_at": utcnow()})
        answer = result.get("raw") or result.get("summary", "")
        now = utcnow()
        history = data.setdefault("historico", [])
        if not data.get("title") or data.get("title") == "Nova conversa":
            data["title"] = " ".join(pedido.split())[:58] or "Nova conversa"
        history.append({"ts": now, "role": "user", "content": pedido})
        history.append(
            {
                "ts": now,
                "role": "assistant",
                "content": answer[:8000],
                "provider": result.get("provider"),
                "model": result.get("model"),
                "tools_used": result.get("tools_used", []),
            }
        )
        memory = data.setdefault("memoria", [])
        fact = self._memory_fact(pedido)
        if fact and fact not in memory:
            memory.append(fact)
            data["memoria"] = memory[-20:]
        data["updated_at"] = now
        self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)

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

    def _compose_request(self, message: str, attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return message
        chunks = [message.strip(), "\n\n[ANEXOS DO USUARIO]"]
        for item in attachments[:6]:
            name = item.get("name", "anexo")
            mime_type = item.get("mime_type", "text/plain")
            kind = item.get("kind", "text")
            content = str(item.get("content", ""))[:12000]
            chunks.append(f"\n## {name} ({kind} | {mime_type})\n{content}")
        return "".join(chunks)
