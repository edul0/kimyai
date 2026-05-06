from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import build_coding_prompt, build_local_prompt_brief, build_prompt_refiner_prompt
from .artifact_parser import parse_kemy_artifact, strip_artifact_wrapper
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
        session_data = self.storage.get_json(f"session:{session_id}", {})
        effective_mode = self._resolve_mode(pedido, mode, session_data)
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
            session_data = self.storage.get_json(f"session:{job.session_id}", {})
            effective_mode = self._resolve_mode(job.pedido, job.modo, session_data)
            if effective_mode != job.modo:
                job.modo = effective_mode
                self.save(job)
            self._event(job, "Kemy", "Lendo a conversa e o contexto.", 15)
            await asyncio.sleep(0)
            history = session_data.get("historico", [])
            memory = session_data.get("memoria", [])
            compact_context = build_context_snapshot(session_data.get("contexto_compacto"), job.pedido)
            attachment_context = prepare_attachments([item.model_dump() if hasattr(item, "model_dump") else item for item in (job.anexos or [])])

            self._event(job, "Kemy", "Lapidando o pedido com a fazedora de prompts.", 24)
            refined_prompt = await self._refine_prompt(job, history, compact_context)

            self._event(job, "Kemy", "Prompt refinado. Enviando para o orquestrador.", 30)
            prompt = build_coding_prompt(job.pedido, job.modo, history, memory, compact_context, refined_prompt=refined_prompt)
            if attachment_context.get("prompt_context"):
                prompt = f"{prompt}\n\n[ANEXOS PROCESSADOS]\n{attachment_context['prompt_context']}"

            if job.modo == "imagem":
                self._event(job, "Kemy", "Pedido visual detectado. Vou gerar a imagem na rota apropriada.", 48)
                result = await self.pollinations.generate(job.pedido)
                result["tools_used"] = ["pollinations"]
                await self._finish_job(job, result)
                return

            if job.modo == "documento":
                self._event(job, "Kemy", "Orquestrador ativo. Passando o trabalho para o sistema de documentos.", 45)
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
                result["pipeline"] = {
                    "prompt_crafter": refined_prompt,
                    "orchestrator_mode": job.modo,
                    "system": "document-service",
                }
                await self._finish_job(job, result)
                return

            self._event(job, "Kemy", "Consultando ferramentas quando necessario.", 42)
            tool_context = await self.tools.enrich(job.pedido, job.modo)
            if tool_context.get("context"):
                prompt = f"{prompt}\n\n[CONTEXTO DE FERRAMENTAS]\n{tool_context['context']}"

            self._event(job, "Kemy", "Orquestrador ativo. Escolhendo o melhor motor gratuito.", 55)
            result = await self.router.generate(
                prompt,
                job.modo,
                attachments=attachment_context["items"],
                visual_items=attachment_context["visual_items"],
            )
            result = self._hydrate_artifacts(job, result)
            result["pipeline"] = {
                "prompt_crafter": refined_prompt,
                "orchestrator_mode": job.modo,
                "system": "router-runtime",
            }
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

    async def _refine_prompt(self, job: JobState, history: list[dict[str, Any]], compact_context: dict[str, Any]) -> str:
        fallback = build_local_prompt_brief(job.pedido, job.modo, compact_context)
        if self.settings.llm_mode != "providers":
            return fallback
        try:
            refined = await self.router.generate(
                build_prompt_refiner_prompt(job.pedido, job.modo, history, compact_context),
                mode="planejamento",
            )
            brief = (refined.get("raw") or "").strip()
            return brief or fallback
        except Exception:
            return fallback

    def _hydrate_artifacts(self, job: JobState, result: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_kemy_artifact(result.get("raw") or "")
        if not parsed:
            return result
        folder = Path("generated_documents") / job.session_id / job.job_id / "artifacts"
        folder.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        preview_url = ""
        for artifact in parsed.files:
            original_path = artifact.path.replace("\\", "/").strip().lstrip("/")
            safe_name = original_path.replace("/", "__") or "index.html"
            target = folder / safe_name
            target.write_text(artifact.content, encoding="utf-8")
            mime_type = "text/plain"
            if safe_name.endswith(".html"):
                mime_type = "text/html"
                if not preview_url or safe_name.lower().endswith("preview.html"):
                    preview_url = f"/api/artefatos/{job.job_id}/{safe_name}"
            elif safe_name.endswith(".js"):
                mime_type = "application/javascript"
            elif safe_name.endswith(".css"):
                mime_type = "text/css"
            elif safe_name.endswith(".json"):
                mime_type = "application/json"
            elif safe_name.endswith(".md"):
                mime_type = "text/markdown"
            language = self._artifact_language(original_path)
            files.append(
                {
                    "name": safe_name,
                    "relative_path": original_path,
                    "path": str(target).replace("\\", "/"),
                    "mime_type": mime_type,
                    "download_url": f"/api/artefatos/{job.job_id}/{safe_name}",
                    "content": artifact.content[:120000],
                    "language": language,
                }
            )
        result = dict(result)
        result["raw"] = strip_artifact_wrapper(result.get("raw") or "")
        result["summary"] = result.get("summary") or f"Artifact `{parsed.title}` gerado com {len(files)} arquivo(s)."
        result["artifact_title"] = parsed.title
        result["files"] = files
        if preview_url:
            result["preview_url"] = preview_url
        used = list(result.get("tools_used") or [])
        if "kemy-artifact" not in used:
            used.append("kemy-artifact")
        result["tools_used"] = used
        return result

    def _artifact_language(self, path: str) -> str:
        suffix = Path(path).suffix.lower().lstrip(".")
        mapping = {
            "html": "html",
            "css": "css",
            "js": "javascript",
            "jsx": "jsx",
            "ts": "typescript",
            "tsx": "tsx",
            "json": "json",
            "md": "markdown",
            "yml": "yaml",
            "yaml": "yaml",
        }
        return mapping.get(suffix, suffix or "text")

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

    def _resolve_mode(self, message: str, current_mode: str, session_data: dict[str, Any] | None = None) -> str:
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
        site_markers = [
            "site",
            "landing page",
            "landing",
            "dashboard",
            "interface",
            "frontend",
            "pagina",
            "página",
            "app web",
            "web app",
            "html",
            "tailwind",
        ]
        if any(marker in lowered for marker in site_markers):
            return "site"
        slide_markers = [
            "slide",
            "slides",
            "apresentacao",
            "apresentação",
            "deck",
            "ppt",
            "pptx",
            "powerpoint",
            "carrossel",
        ]
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
            *slide_markers,
        ]
        if any(marker in lowered for marker in document_markers):
            return "documento"
        if session_data:
            history = session_data.get("historico", [])
            recent_messages = history[-6:]
            recent_text = "\n".join(str(item.get("content") or "") for item in recent_messages).lower()
            recent_files = any((item.get("files") or item.get("result", {}).get("files") or []) for item in recent_messages if item.get("role") == "assistant")
            recent_document_context = recent_files or any(marker in recent_text for marker in document_markers)
            followup_markers = [
                "ajuste",
                "melhore",
                "refaça",
                "refaca",
                "continue",
                "altere",
                "troque",
                "mude",
                "deixa",
                "quero",
                "agora",
                "isso",
                "esse",
                "essa",
            ]
            if recent_document_context and (len(lowered.split()) <= 18 or any(marker in lowered for marker in followup_markers)):
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
