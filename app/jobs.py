from __future__ import annotations

import asyncio
import copy
import hashlib
import html
import json
import re
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents import build_coding_prompt, build_local_prompt_brief, build_prompt_refiner_prompt
from .artifact_parser import parse_kemy_artifact, strip_artifact_wrapper
from .attachment_service import prepare_attachments
from .config import Settings
from .context_memory import build_context_snapshot, split_request_parts
from .document_service import DocumentService
from .github_service import GitHubService
from .intent_planner import build_execution_plan, classify_request_mode
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
        self.github = GitHubService(
            token=settings.github_token,
            default_repo=settings.github_repo_url,
            default_branch=settings.github_default_branch,
            user_name=settings.github_user_name,
            user_email=settings.github_user_email,
        )
        self.response_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def create(self, session_id: str, message: str, mode: str, attachments: list[dict[str, Any]] | None = None, github_repo: str | None = None) -> JobState:
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

    def register_user_turn(self, session_id: str, pedido: str, owner: str | None = None) -> dict[str, Any]:
        key = f"session:{session_id}"
        now = utcnow()
        data = self.storage.get_json(key, {"session_id": session_id, "historico": [], "created_at": now})
        if owner and not data.get("owner"):
            data["owner"] = owner
        compact_context = build_context_snapshot(data.get("contexto_compacto") or {}, pedido)
        request_parts = split_request_parts(pedido)
        history = data.setdefault("historico", [])
        if not (
            history
            and history[-1].get("role") == "user"
            and str(history[-1].get("content", "")).strip() == pedido
        ):
            history.append(
                {
                    "ts": now,
                    "role": "user",
                    "content": pedido,
                    "request_parts": request_parts,
                    "context_snapshot": compact_context,
                }
            )
        data["contexto_compacto"] = compact_context
        if not data.get("title") or data.get("title") == "Nova conversa":
            data["title"] = " ".join(pedido.split())[:58] or "Nova conversa"
        data["updated_at"] = now
        self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)
        return data

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
            await self._event_async(job, "Kemy", "Lendo a conversa e o contexto.", 15)
            await asyncio.sleep(0)
            history = session_data.get("historico", [])
            memory = session_data.get("memoria", [])
            compact_context = build_context_snapshot(session_data.get("contexto_compacto"), job.pedido)
            execution_plan = build_execution_plan(job.pedido, job.modo, compact_context)
            if execution_plan.mode != job.modo:
                job.modo = execution_plan.mode
                self.save(job)
            attachment_context = prepare_attachments([item.model_dump() if hasattr(item, "model_dump") else item for item in (job.anexos or [])])

            await self._event_async(job, "Kemy", "Lapidando o pedido com a fazedora de prompts.", 24)
            refined_prompt = await self._refine_prompt(job, history, compact_context, execution_plan.as_prompt())

            await self._event_async(job, "Kemy", f"Plano definido: {execution_plan.stack}.", 30)
            prompt = build_coding_prompt(
                job.pedido,
                job.modo,
                history,
                memory,
                compact_context,
                refined_prompt=refined_prompt,
                execution_plan=execution_plan.as_prompt(),
            )
            site_context = ""
            if job.modo == "site" and self._is_site_edit_request(job.pedido, session_data):
                site_context = self._build_site_edit_context(job, session_data)
                if site_context:
                    prompt = (
                        f"{prompt}\n\n"
                        "[SITE ATUAL - EDICAO INCREMENTAL]\n"
                        f"{site_context}\n\n"
                        "Regra obrigatoria: evolua o mesmo projeto ja existente no contexto acima.\n"
                        "Aplique somente as mudancas pedidas nesta mensagem e preserve o que ja funciona.\n"
                    )
            if attachment_context.get("prompt_context"):
                prompt = f"{prompt}\n\n[ANEXOS PROCESSADOS]\n{attachment_context['prompt_context']}"
            execution_plan_data = execution_plan.as_dict()
            cache_key = self._response_cache_key(
                job,
                execution_plan_data,
                compact_context,
                attachment_context["items"],
                site_context=site_context,
            )
            cached_response = self._get_cached_response(cache_key) if self.settings.response_cache_enabled else None

            if job.modo == "imagem":
                await self._event_async(job, "Kemy", "Pedido visual detectado. Vou gerar a imagem na rota apropriada.", 48)
                result = await self.pollinations.generate(job.pedido)
                result["tools_used"] = ["pollinations"]
                result["execution_plan"] = execution_plan_data
                await self._finish_job(job, result)
                return

            if job.modo == "documento":
                await self._event_async(job, "Kemy", "Orquestrador ativo. Passando o trabalho para o sistema de documentos.", 45)
                if cached_response:
                    await self._event_async(job, "Kemy", "Rascunho reutilizado do contexto para acelerar a entrega.", 56)
                    draft = cached_response
                    draft["cache"] = {"hit": True}
                else:
                    draft = await self.router.generate(
                        prompt,
                        job.modo,
                        attachments=attachment_context["items"],
                        visual_items=attachment_context["visual_items"],
                    )
                    self._set_cached_response(cache_key, draft)
                await self._event_async(job, "Kemy", "Montando arquivos finais do documento.", 72)
                result = self.documents.generate(job.session_id, job.job_id, job.pedido, draft, plan=execution_plan_data)
                result["tools_used"] = list(
                    dict.fromkeys((draft.get("tools_used") or []) + (result.get("tools_used") or []) + ["python-docx", "reportlab"])
                )
                result["pipeline"] = {
                    "prompt_crafter": refined_prompt,
                    "orchestrator_mode": job.modo,
                    "system": "document-service",
                    "execution_plan": execution_plan_data,
                }
                result["execution_plan"] = execution_plan_data
                await self._finish_job(job, result)
                return

            tool_context: dict[str, Any] = {"used": []}
            if cached_response:
                await self._event_async(job, "Kemy", "Resposta reutilizada do contexto para acelerar a tarefa.", 58)
                result = cached_response
                result["cache"] = {"hit": True}
            else:
                await self._event_async(job, "Kemy", "Consultando ferramentas quando necessario.", 42)
                tool_context = await self.tools.enrich(job.pedido, job.modo)
                if tool_context.get("context"):
                    prompt = f"{prompt}\n\n[CONTEXTO DE FERRAMENTAS]\n{tool_context['context']}"

                await self._event_async(job, "Kemy", "Orquestrador ativo. Escolhendo o melhor motor gratuito.", 55)
                result = await self.router.generate(
                    prompt,
                    job.modo,
                    attachments=attachment_context["items"],
                    visual_items=attachment_context["visual_items"],
                )
                result = await self._maybe_self_review(job, prompt, result, execution_plan.as_prompt())
                if job.modo == "site":
                    result = self._ensure_site_artifact(job, result)
                self._set_cached_response(cache_key, result)
            result = self._hydrate_artifacts(job, result)
            result["pipeline"] = {
                "prompt_crafter": refined_prompt,
                "orchestrator_mode": job.modo,
                "system": "router-runtime",
                "execution_plan": execution_plan_data,
            }
            result["execution_plan"] = execution_plan_data
            tools_used = list(dict.fromkeys((result.get("tools_used") or []) + (tool_context.get("used") or [])))
            if cached_response:
                tools_used.append("response-cache")
            result["tools_used"] = tools_used
            if attachment_context["items"]:
                result["attachments_used"] = [
                    {"name": item["name"], "mime_type": item["mime_type"], "visual": item["visual"]}
                    for item in attachment_context["items"]
                ]

            await self._event_async(job, "Kemy", "Revisando resposta antes de entregar.", 75)
            result.setdefault("security_report", "Nenhum segredo deve ser escrito no repositorio; use variaveis de ambiente.")

            await self._finish_job(job, result)
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            job.status = "error"
            job.etapa = "Erro na execucao"
            job.erro = str(exc)
            job.updated_at = utcnow()
            self.save(job)
            await self.supabase.insert_job(job.model_dump())

    async def _refine_prompt(
        self,
        job: JobState,
        history: list[dict[str, Any]],
        compact_context: dict[str, Any],
        execution_plan: str,
    ) -> str:
        fallback = build_local_prompt_brief(job.pedido, job.modo, compact_context, execution_plan=execution_plan)
        if self.settings.llm_mode != "providers":
            return fallback
        try:
            refined = await self.router.generate(
                build_prompt_refiner_prompt(job.pedido, job.modo, history, compact_context, execution_plan=execution_plan),
                mode="planejamento",
            )
            brief = (refined.get("raw") or "").strip()
            return brief or fallback
        except Exception:
            return fallback

    async def _maybe_self_review(
        self,
        job: JobState,
        prompt: str,
        result: dict[str, Any],
        execution_plan_prompt: str,
    ) -> dict[str, Any]:
        if not self.settings.self_review_enabled:
            return result
        if job.modo not in {"coding", "site"}:
            return result
        if self.settings.llm_mode != "providers":
            return result
        raw = str(result.get("raw") or result.get("summary") or "").strip()
        reason = self._self_review_reason(job.modo, result, raw)
        if not reason:
            return result
        review_prompt = self._build_self_review_prompt(job, prompt, raw, execution_plan_prompt, reason)
        try:
            reviewed = await self.router.generate(review_prompt, mode="auditoria")
            candidate = str(reviewed.get("raw") or reviewed.get("summary") or "").strip()
            if not candidate:
                return result
            if not self._is_review_candidate_better(job.modo, raw, candidate):
                result["qa_autofix"] = {"applied": False, "reason": reason, "discarded": True}
                return result
            merged = dict(result)
            merged["raw"] = candidate[: self.settings.self_review_max_chars]
            merged["summary"] = reviewed.get("summary") or result.get("summary") or "Resposta refinada automaticamente."
            merged["qa_autofix"] = {
                "applied": True,
                "reason": reason,
                "provider": reviewed.get("provider"),
                "model": reviewed.get("model"),
            }
            merged["tools_used"] = list(dict.fromkeys((result.get("tools_used") or []) + ["auto-qa-refiner"]))
            return merged
        except Exception:
            return result

    def _self_review_reason(self, mode: str, result: dict[str, Any], raw: str) -> str:
        lowered = raw.lower()
        if mode == "site":
            files = result.get("files") or []
            has_html_file = any(str(item.get("name", "")).lower().endswith(".html") for item in files)
            has_artifact = "<kemy_artifact" in lowered
            has_html = "<!doctype html" in lowered or "<html" in lowered
            generic = any(marker in lowered for marker in ["concluido", "feito", "pronto"]) and len(raw) < 220
            if (not has_artifact and not has_html and not has_html_file) or generic:
                return "saida-site-fraca"
        if mode == "coding":
            generic = any(marker in lowered for marker in ["nao posso", "não posso", "sem acesso", "nao tenho acesso"])
            if generic and len(raw) < 800:
                return "saida-coding-bloqueada"
            if len(raw) < 260 and not (result.get("files") or result.get("diff")):
                return "saida-coding-curta"
        return ""

    def _build_self_review_prompt(
        self,
        job: JobState,
        prompt: str,
        raw: str,
        execution_plan_prompt: str,
        reason: str,
    ) -> str:
        base = (
            "Voce e Kimi QA Refiner. Reescreva a resposta final para o usuario em alta qualidade, sem explicar o processo.\n"
            "Objetivo: manter o pedido original e corrigir lacunas de entrega.\n"
            "NUNCA devolva analise de auditoria, checklist interno, JSON cru ou metacomentario.\n"
        )
        if job.modo == "site":
            extra = (
                "Obrigatorio para site: responder com <kemy_artifact title=\"...\"> contendo pelo menos:\n"
                "1) <file path=\"preview.html\"> com HTML completo\n"
                "2) <file path=\"README.md\"> com instrucoes de uso\n"
                "Sem texto fora do artifact.\n"
            )
        else:
            extra = (
                "Para coding: entregar resposta tecnica acionavel com diagnostico curto, correcao proposta e trechos de codigo objetivos.\n"
                "Sem desculpas vagas. Sem enrolacao.\n"
            )
        return (
            f"{base}{extra}\n"
            f"Motivo do refino: {reason}\n\n"
            f"Plano esperado:\n{execution_plan_prompt}\n\n"
            f"Pedido original:\n{job.pedido}\n\n"
            f"Prompt de contexto (resumido):\n{prompt[:4500]}\n\n"
            f"Resposta atual que precisa ser melhorada:\n{raw[:6000]}\n"
        )

    def _is_review_candidate_better(self, mode: str, original: str, candidate: str) -> bool:
        original_l = original.lower()
        candidate_l = candidate.lower()
        if mode == "site":
            original_ok = "<kemy_artifact" in original_l or "<html" in original_l
            candidate_ok = "<kemy_artifact" in candidate_l or "<html" in candidate_l
            if candidate_ok and not original_ok:
                return True
        if len(candidate.strip()) >= max(320, int(len(original.strip()) * 1.2)):
            return True
        if "```" in candidate and "```" not in original:
            return True
        return False

    def _build_site_edit_context(self, job: JobState, session_data: dict[str, Any]) -> str:
        snapshot = self._latest_site_snapshot(session_data)
        if not snapshot:
            return ""
        files = snapshot.get("files") or []
        if not files:
            return ""
        budget = 36000
        blocks: list[str] = []
        for file_item in files[:10]:
            path = str(file_item.get("path") or "").replace("\\", "/").strip()
            content = str(file_item.get("content") or "")
            if not path or not content:
                continue
            cleaned = self._clean_artifact_content(path, content).strip()
            if not cleaned:
                continue
            per_file_limit = 12000 if path.lower().endswith(".html") else 7000
            clipped = cleaned[: min(per_file_limit, budget)]
            if not clipped.strip():
                continue
            language = self._artifact_language(path)
            blocks.append(
                f"<file path=\"{path}\">\n"
                f"```{language}\n"
                f"{clipped}\n"
                "```\n"
                "</file>"
            )
            budget -= len(clipped)
            if budget <= 2200:
                break
        if not blocks:
            return ""
        title = str(snapshot.get("title") or self._site_title_from_prompt(job.pedido)).strip()
        summary = str(snapshot.get("summary") or "").strip()
        summary_block = f"Resumo atual: {summary}\n" if summary else ""
        files_block = "\n\n".join(blocks)
        return (
            f"Projeto atual: {title}\n"
            f"{summary_block}"
            "Contexto: existe um site ja criado nesta sessao.\n"
            "Ao responder, evolua esse mesmo projeto sem reiniciar do zero.\n"
            "Se o usuario pedir mudanca visual (ex.: tom, design, tema), aplique no mesmo preview.\n"
            "Se pedir funcionalidade nova, implemente mantendo o que ja existe.\n"
            "Entregue o artifact completo atualizado com preview.html funcional.\n\n"
            f"{files_block}"
        )


    def _is_site_edit_request(self, pedido: str, session_data: dict[str, Any]) -> bool:
        """Determina se o pedido é uma edição incremental ou um novo projeto."""
        text = pedido.lower()
        new_project_signals = ["crie um", "cria um", "crie uma", "cria uma", "fazer um", "fazer uma", "novo site", "nova landing", "novo app", "novo dashboard"]
        if any(sig in text for sig in new_project_signals): return False
        edit_signals = ["muda", "mude", "altera", "altere", "adiciona", "adicione", "remove", "remova", "ajusta", "ajuste", "melhora", "melhore", "esse site", "esse projeto", "o layout"]
        if any(sig in text for sig in edit_signals): return True
        history = (session_data or {}).get("historico") or []
        has_recent_site = any(self._extract_site_files_from_history_item(item) for item in history[-6:] if item.get("role") == "assistant")
        return has_recent_site and len(text.split()) < 20

    def _latest_site_snapshot(self, session_data: dict[str, Any]) -> dict[str, Any] | None:
        history = (session_data or {}).get("historico") or []
        for item in reversed(history[-24:]):
            if item.get("role") != "assistant":
                continue
            candidate = item.get("site_snapshot")
            if isinstance(candidate, dict) and candidate.get("files"):
                return candidate
            files = self._extract_site_files_from_history_item(item)
            if files:
                result_payload = item.get("result") or {}
                title = (
                    str(result_payload.get("artifact_title") or "")
                    or str(result_payload.get("document_title") or "")
                    or str(item.get("summary") or "")
                    or "Projeto Kemy"
                )
                return {"title": title[:90], "summary": str(item.get("content") or "")[:220], "files": files}
        return None

    def _extract_site_files_from_history_item(self, item: dict[str, Any]) -> list[dict[str, str]]:
        candidates: list[list[dict[str, Any]]] = []
        result = item.get("result") or {}
        snapshot = item.get("site_snapshot") or {}
        if isinstance(snapshot, dict):
            snap_files = snapshot.get("files")
            if isinstance(snap_files, list):
                candidates.append(snap_files)
        item_files = item.get("files")
        if isinstance(item_files, list):
            candidates.append(item_files)
        result_files = result.get("files")
        if isinstance(result_files, list):
            candidates.append(result_files)
        merged: dict[str, str] = {}
        for file_list in candidates:
            for file_item in file_list:
                if not isinstance(file_item, dict):
                    continue
                path = str(file_item.get("name") or file_item.get("path") or "").replace("\\", "/").strip().lstrip("/")
                if not path:
                    continue
                lowered = path.lower()
                if not lowered.endswith((".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".md")):
                    continue
                content = str(file_item.get("content") or "")
                if not content.strip():
                    continue
                if path not in merged:
                    merged[path] = content
        if not merged:
            return []
        if not any(path.lower().endswith(".html") for path in merged):
            return []
        priority = [
            "preview.html",
            "index.html",
            "src/main.tsx",
            "src/app.tsx",
            "src/styles.css",
            "src/index.css",
            "style.css",
            "script.js",
            "package.json",
            "readme.md",
        ]

        def _rank(path: str) -> tuple[int, str]:
            lowered = path.lower()
            for idx, key in enumerate(priority):
                if lowered == key:
                    return idx, lowered
            return len(priority) + 1, lowered

        ordered = sorted(merged.items(), key=lambda item: _rank(item[0]))
        budget = 38000
        normalized_files: list[dict[str, str]] = []
        for path, content in ordered[:10]:
            cleaned = self._clean_artifact_content(path, content).strip()
            if not cleaned:
                continue
            per_file_limit = 12000 if path.lower().endswith(".html") else 7000
            clipped = cleaned[: min(per_file_limit, budget)]
            if not clipped.strip():
                continue
            normalized_files.append({"path": path, "content": clipped})
            budget -= len(clipped)
            if budget <= 2000:
                break
        return normalized_files

    def _site_snapshot_from_result(self, result: dict[str, Any]) -> dict[str, Any] | None:
        files_raw = result.get("files")
        if not isinstance(files_raw, list):
            return None
        history_like = {"files": files_raw, "result": {"files": files_raw}}
        files = self._extract_site_files_from_history_item(history_like)
        if not files:
            return None
        title = str(result.get("artifact_title") or result.get("document_title") or result.get("summary") or "Projeto Kemy")
        summary = str(result.get("summary") or "")[:220]
        return {"title": title[:90], "summary": summary, "files": files}

    def _response_cache_key(
        self,
        job: JobState,
        execution_plan: dict[str, Any],
        compact_context: dict[str, Any] | None,
        attachments: list[dict[str, Any]],
        site_context: str = "",
    ) -> str:
        normalized_prompt = re.sub(r"\s+", " ", (job.pedido or "").strip().lower())
        context_summary = str((compact_context or {}).get("summary") or "")[:500]
        attachment_fingerprint = self._attachments_fingerprint(attachments)
        site_context_hash = ""
        if site_context:
            site_context_hash = hashlib.sha256(site_context.encode("utf-8", errors="ignore")).hexdigest()[:20]
        payload = {
            "session_id": job.session_id,
            "mode": job.modo,
            "prompt": normalized_prompt,
            "intent": execution_plan.get("intent"),
            "contract": execution_plan.get("response_contract"),
            "context_summary": context_summary,
            "attachments": attachment_fingerprint,
            "site_context_hash": site_context_hash,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        return f"resp:{job.modo}:{digest}"

    def _attachments_fingerprint(self, attachments: list[dict[str, Any]]) -> list[str]:
        fingerprints: list[str] = []
        for item in attachments or []:
            name = str(item.get("name") or "")
            mime_type = str(item.get("mime_type") or "")
            content = str(item.get("content") or "")
            mini = content[:600]
            digest = hashlib.sha256(mini.encode("utf-8", errors="ignore")).hexdigest()[:16]
            fingerprints.append(f"{name}|{mime_type}|{digest}")
        return fingerprints

    def _get_cached_response(self, key: str) -> dict[str, Any] | None:
        self._prune_response_cache()
        cached = self.response_cache.get(key)
        if not cached:
            return None
        expires_at, payload = cached
        if expires_at <= time.time():
            self.response_cache.pop(key, None)
            return None
        return copy.deepcopy(payload)

    def _set_cached_response(self, key: str, payload: dict[str, Any]) -> None:
        ttl = max(30, int(self.settings.response_cache_ttl_seconds or 3600))
        self.response_cache[key] = (time.time() + ttl, copy.deepcopy(payload))
        self._prune_response_cache()

    def _prune_response_cache(self) -> None:
        now = time.time()
        for cache_key, (expires_at, _payload) in list(self.response_cache.items()):
            if expires_at <= now:
                self.response_cache.pop(cache_key, None)
        max_items = 320
        if len(self.response_cache) <= max_items:
            return
        sorted_items = sorted(self.response_cache.items(), key=lambda item: item[1][0])
        for cache_key, _value in sorted_items[: max(0, len(sorted_items) - max_items)]:
            self.response_cache.pop(cache_key, None)

    def _hydrate_artifacts(self, job: JobState, result: dict[str, Any]) -> dict[str, Any]:
        parsed = parse_kemy_artifact(result.get("raw") or "")
        if not parsed:
            return result
        folder = Path("generated_documents") / job.session_id / job.job_id / "artifacts"
        folder.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        preview_url = ""
        preview_rank = 99
        recovered_html = self._extract_html_candidate(str(result.get("raw") or ""))
        for artifact in parsed.files:
            original_path = artifact.path.replace("\\", "/").strip().lstrip("/")
            content = self._clean_artifact_content(original_path, artifact.content)
            safe_name = original_path.replace("/", "__") or "index.html"
            if safe_name.lower().endswith(".html") and not self._is_renderable_preview_html(content):
                if recovered_html and self._is_renderable_preview_html(recovered_html):
                    content = recovered_html
                else:
                    content = self._minimal_preview_html(job.pedido, parsed.title)
            target = folder / safe_name
            target.write_text(content, encoding="utf-8")
            mime_type = "text/plain"
            if safe_name.endswith(".html"):
                mime_type = "text/html"
                candidate_rank = 2
                lowered_name = safe_name.lower()
                if lowered_name.endswith("preview.html"):
                    candidate_rank = 0
                elif lowered_name.endswith("slides.html") or lowered_name.endswith(".slides.html"):
                    candidate_rank = 0
                elif lowered_name.endswith("index.html"):
                    candidate_rank = 1
                if not preview_url or candidate_rank < preview_rank:
                    preview_rank = candidate_rank
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
                    "content": content[:120000],
                    "language": language,
                }
            )
        manifest = {
            "artifact_title": parsed.title,
            "job_id": job.job_id,
            "session_id": job.session_id,
            "preview_url": preview_url,
            "files": [
                {
                    "name": item["name"],
                    "relative_path": item["relative_path"],
                    "language": item["language"],
                    "download_url": item["download_url"],
                }
                for item in files
            ],
        }
        manifest_path = folder / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        files.append(
            {
                "name": "manifest.json",
                "relative_path": "manifest.json",
                "path": str(manifest_path).replace("\\", "/"),
                "mime_type": "application/json",
                "download_url": f"/api/artefatos/{job.job_id}/manifest.json",
                "content": manifest_path.read_text(encoding="utf-8")[:120000],
                "language": "json",
            }
        )
        archive_path = folder / "projeto-kemy.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                path = Path(str(item["path"]))
                if path.exists():
                    archive.write(path, arcname=str(item["relative_path"]).replace("\\", "/"))
        files.append(
            {
                "name": "projeto-kemy.zip",
                "relative_path": "projeto-kemy.zip",
                "path": str(archive_path).replace("\\", "/"),
                "mime_type": "application/zip",
                "download_url": f"/api/artefatos/{job.job_id}/projeto-kemy.zip",
                "content": "",
                "language": "zip",
            }
        )
        result = dict(result)
        result["raw"] = strip_artifact_wrapper(result.get("raw") or "")
        result["summary"] = result.get("summary") or f"Projeto `{parsed.title}` gerado com preview, arquivos e ZIP para download."
        result["artifact_title"] = parsed.title
        result["files"] = files
        if preview_url:
            result["preview_url"] = preview_url
            result["site_url"] = preview_url
        result["project_archive_url"] = f"/api/artefatos/{job.job_id}/projeto-kemy.zip"
        used = list(result.get("tools_used") or [])
        if "kemy-artifact" not in used:
            used.append("kemy-artifact")
        result["tools_used"] = used
        return result

    def _ensure_site_artifact(self, job: JobState, result: dict[str, Any]) -> dict[str, Any]:
        raw = result.get("raw") or result.get("summary") or ""
        parsed = parse_kemy_artifact(raw)
        if (parsed and self._artifact_has_usable_html(parsed.files)) or result.get("files"):
            return result
        html_candidate = self._extract_html_candidate(raw)
        if html_candidate and self._is_renderable_preview_html(html_candidate):
            result = dict(result)
            result["raw"] = self._site_artifact_from_html(job.pedido, html_candidate)
            result["summary"] = "Site convertido para artifact com live preview, arquivos e ZIP para download."
            tools = list(result.get("tools_used") or [])
            if "kemy-site-html-repair" not in tools:
                tools.append("kemy-site-html-repair")
            result["tools_used"] = tools
            return result
        result = dict(result)
        result["raw"] = self._fallback_site_artifact(job.pedido)
        result["summary"] = "Site gerado com live preview, arquivos e ZIP para download."
        tools = list(result.get("tools_used") or [])
        if "kemy-site-fallback" not in tools:
            tools.append("kemy-site-fallback")
        result["tools_used"] = tools
        return result

    def _artifact_has_usable_html(self, files: list[Any]) -> bool:
        for file_item in files:
            path = str(getattr(file_item, "path", "")).lower()
            if not path.endswith(".html"):
                continue
            content = self._clean_artifact_content(path, str(getattr(file_item, "content", "")))
            if self._is_renderable_preview_html(content):
                return True
        return False

    def _extract_html_candidate(self, raw: str) -> str:
        text = html.unescape((raw or "").strip())
        fenced = re.search(r"```html\s*(?P<html>[\s\S]*?)```", text, re.IGNORECASE)
        if fenced:
            return fenced.group("html").strip()
        doctype = re.search(r"<!doctype html[\s\S]*?</html\s*>", text, re.IGNORECASE)
        if doctype:
            return doctype.group(0).strip()
        html_doc = re.search(r"<html\b[\s\S]*?</html\s*>", text, re.IGNORECASE)
        if html_doc:
            return html_doc.group(0).strip()
        return ""

    def _looks_like_html_document(self, content: str) -> bool:
        lowered = (content or "").lower()
        return ("<html" in lowered or "<!doctype html" in lowered) and "</html" in lowered

    def _is_renderable_preview_html(self, content: str) -> bool:
        if not self._looks_like_html_document(content):
            return False
        lowered = (content or "").lower()
        vite_shell = bool(
            re.search(r"<div[^>]+id=['\"](?:root|app)['\"][^>]*>\s*</div>", lowered)
            and re.search(r"<script[^>]+type=['\"]module['\"][^>]+src=['\"][^'\"]*(?:/src/|main\.(?:tsx|jsx|ts|js))", lowered)
        )
        if vite_shell:
            return False
        has_ui = bool(re.search(r"<(main|section|article|form|h1|h2|p|button|nav|header)\b", lowered))
        if not has_ui and re.search(r"<div[^>]+id=['\"](?:root|app)['\"][^>]*>\s*</div>", lowered):
            return False
        return True

    def _site_artifact_from_html(self, pedido: str, html_doc: str) -> str:
        title = self._site_title_from_prompt(pedido)
        readme = f"""# {title}

Site recuperado automaticamente pela Kemy a partir de HTML gerado pelo modelo.

## Como usar

- Abra `preview.html` para visualizar imediatamente.
- Baixe `projeto-kemy.zip` para obter todos os arquivos.
- Se quiser transformar em React/Vite depois, peça para a Kemy evoluir este preview para projeto completo.
"""
        return (
            f"<kemy_artifact title=\"{html.escape(title, quote=True)}\">\n"
            "<file path=\"preview.html\">\n"
            f"{html_doc.strip()}\n"
            "</file>\n"
            "<file path=\"README.md\">\n"
            f"{readme}\n"
            "</file>\n"
            "</kemy_artifact>"
        )

    def _clean_artifact_content(self, path: str, content: str) -> str:
        text = html.unescape((content or "").strip())
        fenced = text
        if fenced.startswith("```"):
            lines = fenced.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            fenced = "\n".join(lines).strip()
        if path.lower().endswith(".html") and fenced.lower().startswith("html"):
            fenced = fenced[4:].strip()
        return fenced

    def _minimal_preview_html(self, pedido: str, title: str) -> str:
        safe_title = html.escape(title or self._site_title_from_prompt(pedido))
        safe_prompt = html.escape(" ".join((pedido or "").split()))
        return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: linear-gradient(135deg, #f5fbff 0%, #e8f2ff 100%);
      color: #10243d;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 28px;
    }}
    main {{
      width: min(960px, 100%);
      border-radius: 22px;
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid rgba(16, 36, 61, 0.12);
      box-shadow: 0 20px 44px rgba(16, 36, 61, 0.12);
      padding: 34px;
    }}
    h1 {{
      margin: 0 0 12px;
      font-size: 36px;
      letter-spacing: -0.03em;
    }}
    p {{
      margin: 0;
      font-size: 18px;
      line-height: 1.55;
      color: #40546f;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    <p>Preview reconstruido automaticamente pela Kemy para manter visualizacao e download validos.</p>
    <p style="margin-top:12px;">Pedido original: {safe_prompt}</p>
  </main>
</body>
</html>"""

    def _fallback_site_artifact(self, pedido: str) -> str:
        title = self._site_title_from_prompt(pedido)
        safe_title = html.escape(title)
        prompt_text = html.escape(" ".join(pedido.split()))
        html_doc = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    :root {{
      --ink: #10231d;
      --muted: #5f756e;
      --brand: #0f8f70;
      --brand-dark: #0d5f50;
      --cream: #f6f1e8;
      --card: rgba(255, 255, 255, 0.82);
      font-family: "Segoe UI", system-ui, sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 18% 12%, rgba(15, 143, 112, .18), transparent 34%),
        linear-gradient(135deg, #fbf8f0 0%, #eef7f2 45%, #d9efe7 100%);
      min-height: 100vh;
    }}
    header {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 28px 0;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }}
    .logo {{ font-weight: 900; letter-spacing: -.04em; font-size: 28px; }}
    nav {{ display: flex; gap: 18px; color: var(--muted); font-weight: 700; }}
    .hero {{
      width: min(1180px, calc(100% - 32px));
      margin: 32px auto;
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 28px;
      align-items: stretch;
    }}
    .panel {{
      border: 1px solid rgba(16, 35, 29, .12);
      border-radius: 34px;
      background: var(--card);
      box-shadow: 0 28px 80px rgba(16, 35, 29, .14);
      backdrop-filter: blur(18px);
    }}
    .copy {{ padding: clamp(32px, 6vw, 72px); }}
    .eyebrow {{
      display: inline-flex;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(15, 143, 112, .1);
      color: var(--brand-dark);
      font-size: 13px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: .12em;
    }}
    h1 {{
      margin: 22px 0;
      font-size: clamp(42px, 7vw, 82px);
      line-height: .94;
      letter-spacing: -.07em;
    }}
    .lead {{
      max-width: 650px;
      color: var(--muted);
      font-size: clamp(18px, 2vw, 24px);
      line-height: 1.45;
    }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 34px; }}
    .btn {{
      border: 0;
      border-radius: 999px;
      padding: 16px 22px;
      font-weight: 900;
      text-decoration: none;
      cursor: pointer;
    }}
    .btn.primary {{ background: var(--ink); color: white; }}
    .btn.secondary {{ background: white; color: var(--ink); border: 1px solid rgba(16,35,29,.12); }}
    .booking {{ padding: 28px; display: grid; gap: 18px; }}
    .booking h2 {{ margin: 0; font-size: 28px; letter-spacing: -.04em; }}
    .grid {{ display: grid; gap: 12px; }}
    label {{ display: grid; gap: 7px; color: var(--muted); font-size: 13px; font-weight: 800; }}
    input, select {{
      width: 100%;
      border: 1px solid rgba(16,35,29,.14);
      border-radius: 18px;
      padding: 15px 16px;
      font: inherit;
      background: rgba(255,255,255,.72);
    }}
    .services {{
      width: min(1180px, calc(100% - 32px));
      margin: 28px auto 70px;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 18px;
    }}
    .service {{ padding: 24px; }}
    .service strong {{ display: block; font-size: 22px; margin-bottom: 8px; }}
    .service span {{ color: var(--muted); line-height: 1.45; }}
    @media (max-width: 860px) {{
      header, nav {{ align-items: flex-start; }}
      nav {{ display: none; }}
      .hero, .services {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="logo">{safe_title}</div>
    <nav><span>Serviços</span><span>Agenda</span><span>Contato</span></nav>
  </header>
  <main>
    <section class="hero">
      <div class="panel copy">
        <span class="eyebrow">Agenda online</span>
        <h1>{safe_title}</h1>
        <p class="lead">Uma experiência elegante para clientes escolherem o serviço, reservarem horário e chegarem no barbeiro com tudo combinado.</p>
        <div class="actions">
          <a class="btn primary" href="#agendar">Agendar corte</a>
          <a class="btn secondary" href="#servicos">Ver serviços</a>
        </div>
      </div>
      <form id="agendar" class="panel booking">
        <h2>Reserve seu horário</h2>
        <div class="grid">
          <label>Nome<input placeholder="Seu nome" /></label>
          <label>Serviço<select><option>Corte masculino</option><option>Barba completa</option><option>Corte + barba</option></select></label>
          <label>Data<input type="date" /></label>
          <label>Horário<input type="time" /></label>
        </div>
        <button class="btn primary" type="button" onclick="alert('Agendamento simulado com sucesso!')">Confirmar agenda</button>
      </form>
    </section>
    <section id="servicos" class="services">
      <article class="panel service"><strong>Corte alinhado</strong><span>Acabamento preciso, consultoria de estilo e finalização premium.</span></article>
      <article class="panel service"><strong>Barba completa</strong><span>Toalha quente, desenho limpo e cuidado para pele sensível.</span></article>
      <article class="panel service"><strong>Agenda prática</strong><span>Fluxo simples para escolher data, horário e serviço sem atrito.</span></article>
    </section>
  </main>
  <script>console.log("Preview Kemy:", "{prompt_text}");</script>
</body>
</html>"""
        readme = f"""# {title}

Site gerado automaticamente pela Kemy para: {pedido}

## Como usar

- Abra `preview.html` para visualizar imediatamente.
- Use o ZIP para baixar todos os arquivos gerados.
- O formulario usa agendamento simulado e pode ser conectado depois ao Supabase.
"""
        return (
            f"<kemy_artifact title=\"{html.escape(title, quote=True)}\">\n"
            "<file path=\"preview.html\">\n"
            f"{html_doc}\n"
            "</file>\n"
            "<file path=\"README.md\">\n"
            f"{readme}\n"
            "</file>\n"
            "</kemy_artifact>"
        )

    def _site_title_from_prompt(self, pedido: str) -> str:
        text = " ".join(pedido.split()).strip(" .")
        lowered = text.lower()
        if "barbe" in lowered or "corte" in lowered or "cabelo" in lowered:
            return "Barbearia Agenda"
        if "imobili" in lowered:
            return "Imobiliaria Prime"
        if "restaurante" in lowered or "comida" in lowered:
            return "Mesa Reservada"
        return (text[:48].strip() or "Site Kemy").title()

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

    async def _event_async(self, job: JobState, agente: str, msg: str, progresso: int) -> None:
        self._event(job, agente, msg, progresso)
        await self.supabase.insert_job(job.model_dump())

    def _compact_file_entry(self, file_item: dict[str, Any], include_content: bool = False, content_limit: int = 0) -> dict[str, Any]:
        payload = {
            "name": file_item.get("name"),
            "relative_path": file_item.get("relative_path"),
            "path": file_item.get("path"),
            "mime_type": file_item.get("mime_type"),
            "download_url": file_item.get("download_url"),
            "language": file_item.get("language"),
        }
        size_value = file_item.get("size_bytes")
        if size_value is not None:
            payload["size_bytes"] = size_value
        if include_content and content_limit > 0:
            content = str(file_item.get("content") or "")
            if content:
                payload["content"] = content[:content_limit]
        return payload

    def _compact_files(self, files: list[dict[str, Any]], include_content: bool = False, content_limit: int = 0) -> list[dict[str, Any]]:
        compacted: list[dict[str, Any]] = []
        for item in files or []:
            if not isinstance(item, dict):
                continue
            compacted.append(self._compact_file_entry(item, include_content=include_content, content_limit=content_limit))
        return compacted

    def _display_answer_for_history(self, result: dict[str, Any]) -> str:
        files = result.get("files") or []
        summary = str(result.get("summary") or "").strip()
        if files:
            return summary or "Entrega concluida com arquivos prontos para preview/download."
        raw = str(result.get("raw") or "").strip()
        return raw or summary or "Concluido."

    def _compact_result_metadata(self, result: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        files = self._compact_files(result.get("files") or [])
        metadata = {
            "summary": result.get("summary"),
            "document_title": result.get("document_title"),
            "artifact_title": result.get("artifact_title"),
            "provider": result.get("provider"),
            "model": result.get("model"),
            "tools_used": list(result.get("tools_used") or []),
            "preview_url": result.get("preview_url"),
            "project_archive_url": result.get("project_archive_url"),
            "image_url": result.get("image_url"),
            "files": files,
        }
        if mode:
            metadata["mode"] = mode
        execution_plan = result.get("execution_plan")
        if isinstance(execution_plan, dict):
            metadata["execution_plan"] = {
                "mode": execution_plan.get("mode"),
                "intent": execution_plan.get("intent"),
                "document_kind": execution_plan.get("document_kind"),
            }
        attachments_used = result.get("attachments_used")
        if isinstance(attachments_used, list):
            metadata["attachments_used"] = attachments_used[:8]
        pipeline = result.get("pipeline")
        if isinstance(pipeline, dict):
            metadata["pipeline"] = {
                "orchestrator_mode": pipeline.get("orchestrator_mode"),
                "system": pipeline.get("system"),
            }
        return metadata

    def _safe_image_data_url(self, value: Any, max_chars: int = 180_000) -> str:
        data_url = str(value or "")
        if not data_url.startswith("data:"):
            return ""
        if len(data_url) > max_chars:
            return ""
        return data_url

    def _append_history(
        self,
        session_id: str,
        pedido: str,
        result: dict[str, Any],
        mode: str | None = None,
    ) -> tuple[dict[str, Any], bool, dict[str, Any], dict[str, Any]]:
        key = f"session:{session_id}"
        data = self.storage.get_json(key, {"session_id": session_id, "historico": [], "created_at": utcnow()})
        answer = self._display_answer_for_history(result)
        now = utcnow()
        history = data.setdefault("historico", [])
        previous_context = data.get("contexto_compacto") or {}
        request_parts = split_request_parts(pedido)
        added_user = False
        last_item = history[-1] if history else {}
        same_user_turn = last_item.get("role") == "user" and str(last_item.get("content", "")).strip() == pedido
        if same_user_turn:
            user_entry = dict(last_item)
            user_context = user_entry.get("context_snapshot") or build_context_snapshot(previous_context, pedido)
            user_entry["context_snapshot"] = user_context
            history[-1] = user_entry
        else:
            user_context = build_context_snapshot(previous_context, pedido)
            user_entry = {
                "ts": now,
                "role": "user",
                "content": pedido,
                "request_parts": request_parts,
                "context_snapshot": user_context,
            }
            history.append(user_entry)
            added_user = True
        assistant_context = build_context_snapshot(user_context, pedido, result)
        if not data.get("title") or data.get("title") == "Nova conversa":
            data["title"] = " ".join(pedido.split())[:58] or "Nova conversa"
        site_snapshot = self._site_snapshot_from_result(result)
        public_files = self._compact_files(result.get("files") or [])
        result_metadata = self._compact_result_metadata(result, mode=mode)
        assistant_entry = {
            "ts": now,
            "role": "assistant",
            "content": answer[:8000],
            "context_snapshot": assistant_context,
            "provider": result.get("provider"),
            "model": result.get("model"),
            "tools_used": result.get("tools_used", []),
            "image_url": result.get("image_url"),
            "image_data_url": self._safe_image_data_url(result.get("image_data_url")),
            "files": public_files,
            "preview_url": result.get("preview_url"),
            "result": result_metadata,
        }
        if site_snapshot:
            assistant_entry["site_snapshot"] = site_snapshot
            assistant_entry["result"]["site_snapshot"] = site_snapshot
        history.append(assistant_entry)
        memory = data.setdefault("memoria", [])
        fact = self._memory_fact(pedido)
        if fact and fact not in memory:
            memory.append(fact)
            data["memoria"] = memory[-20:]
        data["contexto_compacto"] = assistant_context
        if mode:
            data["last_mode"] = mode
        data["updated_at"] = now
        self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)
        return data, added_user, user_entry, assistant_entry

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
        return classify_request_mode(message, current_mode, session_data)

    async def _finish_job(self, job: JobState, result: dict[str, Any]) -> None:
        self._event(job, "Kemy", "Finalizando mensagem.", 92)
        job.status = "done"
        job.etapa = "Concluido"
        job.progresso = 100
        job.resultado = result
        job.updated_at = utcnow()
        self.save(job)
        await self.supabase.insert_job(job.model_dump())
        session_data, added_user, user_message, assistant_message = self._append_history(
            job.session_id,
            job.pedido,
            result,
            mode=job.modo,
        )
        await self.supabase.insert_session(
            job.session_id,
            owner_email=session_data.get("owner"),
            title=session_data.get("title") or "Nova sessao",
            created_at=session_data.get("created_at"),
            updated_at=session_data.get("updated_at"),
        )
        if added_user:
            await self.supabase.insert_message(
                job.session_id,
                "user",
                job.pedido,
                {
                    "request_parts": user_message.get("request_parts", []),
                    "context_snapshot": user_message.get("context_snapshot", {}),
                },
            )
        assistant_metadata = self._compact_result_metadata(result, mode=job.modo)
        assistant_metadata["context_snapshot"] = assistant_message.get("context_snapshot", {})
        if assistant_message.get("site_snapshot"):
            assistant_metadata["site_snapshot"] = assistant_message.get("site_snapshot")
        await self.supabase.insert_message(
            job.session_id,
            "assistant",
            self._display_answer_for_history(result),
            assistant_metadata,
        )
        for file_item in result.get("files") or []:
            await self.supabase.insert_generated_file(job.job_id, file_item)
