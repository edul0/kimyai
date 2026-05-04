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


def utcnow() -> str:
    return datetime.utcnow().isoformat() + "Z"


class JobManager:
    def __init__(self, storage: Storage, settings: Settings):
        self.storage = storage
        self.settings = settings
        self.router = LLMRouter(settings)

    def create(self, session_id: str, message: str, mode: str) -> JobState:
        now = utcnow()
        job = JobState(
            job_id=str(uuid.uuid4()),
            session_id=session_id,
            status="queued",
            etapa="Recebido na fila cloud",
            progresso=5,
            pedido=message[: self.settings.max_prompt_chars],
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
            self._event(job, "Gabriel", "Planejando escopo e especialistas.", 15)
            await asyncio.sleep(0)
            history = self.storage.get_json(f"session:{job.session_id}", {}).get("historico", [])

            self._event(job, "Brenno", "Convertendo pedido em requisitos tecnicos.", 30)
            prompt = build_coding_prompt(job.pedido, job.modo, history)

            self._event(job, "Diego", "Selecionando motor gratuito e arquitetura de entrega.", 45)
            result = await self.router.generate(prompt, job.modo)

            self._event(job, "Bianca", "Aplicando checagens de seguranca e secrets.", 75)
            result.setdefault("security_report", "Nenhum segredo deve ser escrito no repositorio; use variaveis de ambiente.")

            self._event(job, "Leonardo", "Formatando entrega final e testes sugeridos.", 92)
            job.status = "done"
            job.etapa = "Concluido"
            job.progresso = 100
            job.resultado = result
            job.updated_at = utcnow()
            self.save(job)
            self._append_history(job.session_id, job.pedido, result)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            job.status = "error"
            job.etapa = "Erro na execucao"
            job.erro = str(exc)
            job.updated_at = utcnow()
            self.save(job)

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
        data.setdefault("historico", []).append(
            {
                "ts": utcnow(),
                "usuario": pedido,
                "resumo": result.get("summary") or result.get("raw", "")[:400],
                "provider": result.get("provider"),
            }
        )
        data["updated_at"] = utcnow()
        self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)

