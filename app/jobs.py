from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import html
import json
import re
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agents import build_coding_prompt, build_local_prompt_brief, build_prompt_refiner_prompt
from .agent_runtime import SolutionHistory, StrongSandboxRuntime, confidence_from_evidence
from .artifact_parser import parse_kemy_artifact, strip_artifact_wrapper
from .attachment_service import prepare_attachments
from .code_intelligence import RepositoryContext, RepositoryIntelligence, SafeCodeValidator, reasoning_budget
from .config import Settings
from .context_memory import build_context_snapshot, split_request_parts
from .document_service import DocumentService
from .evaluation import DeliveryEvaluator
from .frontend_runtime import BrowserReport, FrontendBrowserRuntime
from .github_service import GitHubService
from .intent_planner import build_execution_plan, classify_request_mode, validate_request_intent
from .llm_router import LLMRouter
from .pollinations import PollinationsImageService
from .schemas import JobState
from .storage import Storage
from .supabase_store import SupabaseStore
from .tools import ExternalTools


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class JobCanceledError(Exception):
    """Raised when a user requested job cancellation."""


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
        self.code_validator = SafeCodeValidator()
        self.frontend_browser = FrontendBrowserRuntime()
        self.solution_history = SolutionHistory(storage)
        self.delivery_evaluator = DeliveryEvaluator()
        self.strong_sandbox = StrongSandboxRuntime()

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
            github_repo=(github_repo or "").strip() or None,
            created_at=now,
            updated_at=now,
        )
        self.save(job)
        return job

    def register_user_turn(
        self,
        session_id: str,
        pedido: str,
        owner: str | None = None,
        github_repo: str | None = None,
    ) -> dict[str, Any]:
        key = f"session:{session_id}"
        now = utcnow()
        data = self.storage.get_json(key, {"session_id": session_id, "historico": [], "created_at": now})
        if owner and not data.get("owner"):
            data["owner"] = owner
        repo_value = (github_repo or "").strip()
        if repo_value:
            data["last_github_repo"] = repo_value
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

    def _cancel_key(self, job_id: str) -> str:
        return f"job_cancel:{job_id}"

    def _is_cancel_requested(self, job_id: str) -> bool:
        flag = self.storage.get_json(self._cancel_key(job_id), {})
        return bool(flag and flag.get("requested"))

    def cancel(self, job_id: str, requested_by: str = "user") -> JobState | None:
        job = self.get(job_id)
        if not job:
            return None
        if job.status in {"done", "error", "canceled"}:
            return job
        self.storage.set_json(
            self._cancel_key(job_id),
            {
                "requested": True,
                "requested_by": requested_by,
                "requested_at": utcnow(),
            },
            ttl=self.settings.job_ttl_seconds,
        )
        cancel_msg = "Execucao cancelada pelo usuario."
        if not any(str(event.get("msg") or "").lower() == cancel_msg.lower() for event in job.eventos):
            job.eventos.append(
                {
                    "ts": utcnow(),
                    "agente": "Kemy",
                    "msg": cancel_msg,
                    "progresso": min(max(job.progresso, 1), 98),
                }
            )
        job.status = "canceled"
        job.etapa = "Cancelado pelo usuario"
        job.erro = cancel_msg
        job.updated_at = utcnow()
        self.save(job)
        return job

    def _raise_if_canceled(self, job: JobState) -> None:
        if self._is_cancel_requested(job.job_id):
            raise JobCanceledError("Execucao cancelada pelo usuario.")

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
            self._raise_if_canceled(job)
            session_data = self.storage.get_json(f"session:{job.session_id}", {})
            intent_validation = validate_request_intent(job.pedido, job.modo, session_data)
            effective_mode = intent_validation.mode
            if effective_mode != job.modo:
                job.modo = effective_mode
                self.save(job)
            await self._event_async(job, "Kemy", "Lendo a conversa e o contexto.", 15)
            self._raise_if_canceled(job)
            await self._event_async(job, "Validador", intent_validation.event_text(), 18)
            self._raise_if_canceled(job)
            await asyncio.sleep(0)
            history = session_data.get("historico", [])
            memory = session_data.get("memoria", [])
            compact_context = build_context_snapshot(session_data.get("contexto_compacto"), job.pedido)
            execution_plan = build_execution_plan(job.pedido, job.modo, compact_context)
            if execution_plan.mode != job.modo:
                job.modo = execution_plan.mode
                self.save(job)
            reasoning_trace = self._build_reasoning_trace(job.pedido, job.modo)
            if reasoning_trace:
                await self._event_async(job, "Kemy", f"Raciocinio tecnico: {reasoning_trace}", 20)
                self._raise_if_canceled(job)
            attachment_context = prepare_attachments([item.model_dump() if hasattr(item, "model_dump") else item for item in (job.anexos or [])])
            learning_context = self._build_error_learning_context(session_data)
            validation_prompt = intent_validation.as_prompt()

            await self._event_async(job, "Kemy", "Lapidando o pedido com a fazedora de prompts.", 24)
            self._raise_if_canceled(job)
            execution_plan_prompt = f"{validation_prompt}\n\n{execution_plan.as_prompt()}"
            if learning_context:
                execution_plan_prompt = f"{execution_plan_prompt}\n\n{learning_context}"
            refined_prompt = await self._refine_prompt(job, history, compact_context, execution_plan_prompt)
            self._raise_if_canceled(job)

            await self._event_async(job, "Kemy", f"Plano definido: {execution_plan.stack}.", 30)
            prompt = build_coding_prompt(
                job.pedido,
                job.modo,
                history,
                memory,
                compact_context,
                refined_prompt=refined_prompt,
                execution_plan=execution_plan_prompt,
            )
            if job.github_repo:
                repo_hint = self._repo_display_name(job.github_repo)
                prompt = (
                    f"{prompt}\n\n"
                    "[REPOSITORIO ALVO]\n"
                    f"- URL: {job.github_repo}\n"
                    f"- Nome: {repo_hint}\n"
                    "- Regra obrigatoria: trabalhar neste repositorio/alvo e evitar gerar template generico fora do contexto.\n"
                    "- Se o pedido for de correcao/manutencao, priorize patch de codigo (modo coding) em vez de criar um site novo.\n"
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
            workspace_context = ""
            if job.modo == "coding" and self._is_workspace_edit_request(job.pedido, session_data):
                workspace_context = self._build_workspace_edit_context(job, session_data)
                if workspace_context:
                    prompt = (
                        f"{prompt}\n\n"
                        "[WORKSPACE ATUAL - EDICAO INCREMENTAL]\n"
                        f"{workspace_context}\n\n"
                        "Regra obrigatoria: atualize o mesmo workspace de codigo do contexto acima.\n"
                        "Preserve o que funciona e aplique somente o pedido atual.\n"
                    )
            repository_context: RepositoryContext | None = None
            if job.modo == "coding":
                try:
                    repository_context = RepositoryIntelligence(self.settings.workspace_root).build_context(job.pedido)
                except (OSError, ValueError):
                    repository_context = None
                if repository_context and repository_context.tree:
                    technical_memory = session_data.get("technical_memory") or {}
                    project_id = hashlib.sha256(repository_context.root.encode("utf-8")).hexdigest()[:20]
                    previous_solutions = self.solution_history.relevant(project_id, job.pedido)
                    prompt = (
                        f"{prompt}\n\n"
                        "[CONTEXTO REAL DO REPOSITORIO - FONTE DE VERDADE]\n"
                        f"{repository_context.as_prompt()}\n\n"
                        "[MEMORIA TECNICA DA SESSAO]\n"
                        f"{json.dumps(technical_memory, ensure_ascii=False)[:5000]}\n\n"
                        "[SOLUCOES ANTERIORES COM EVIDENCIA]\n"
                        f"{json.dumps(previous_solutions, ensure_ascii=False)[:5000]}\n\n"
                        "Nao invente arquivo, simbolo, rota ou dependencia que nao apareca no contexto. "
                        "Se criar algo novo, marque claramente como novo e justifique.\n"
                    )
            if attachment_context.get("prompt_context"):
                prompt = f"{prompt}\n\n[ANEXOS PROCESSADOS]\n{attachment_context['prompt_context']}"
            execution_plan_data = execution_plan.as_dict()
            cache_key = self._response_cache_key(
                job,
                execution_plan_data,
                compact_context,
                attachment_context["items"],
                site_context=f"{site_context}\n{workspace_context}".strip(),
            )
            cached_response = self._get_cached_response(cache_key) if self.settings.response_cache_enabled else None

            if job.modo == "imagem":
                await self._event_async(job, "Kemy", "Pedido visual detectado. Vou gerar a imagem na rota apropriada.", 48)
                self._raise_if_canceled(job)
                result = await self.pollinations.generate(job.pedido)
                self._raise_if_canceled(job)
                result = await self._review_generated_image(job, result)
                result["tools_used"] = ["pollinations"] + (
                    ["visual-art-director"] if result.get("visual_qa", {}).get("provider") else []
                )
                result["execution_plan"] = execution_plan_data
                result["intent_validation"] = intent_validation.as_dict()
                await self._finish_job(job, result)
                return

            if job.modo == "documento":
                await self._event_async(job, "Kemy", "Orquestrador ativo. Passando o trabalho para o sistema de documentos.", 45)
                self._raise_if_canceled(job)
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
                self._raise_if_canceled(job)
                await self._event_async(job, "Kemy", "Montando arquivos finais do documento.", 72)
                self._raise_if_canceled(job)
                result = self.documents.generate(job.session_id, job.job_id, job.pedido, draft, plan=execution_plan_data)
                self._raise_if_canceled(job)
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
                result["intent_validation"] = intent_validation.as_dict()
                await self._finish_job(job, result)
                return

            tool_context: dict[str, Any] = {"used": []}
            if cached_response:
                await self._event_async(job, "Kemy", "Resposta reutilizada do contexto para acelerar a tarefa.", 58)
                result = cached_response
                result["cache"] = {"hit": True}
            else:
                await self._event_async(job, "Kemy", "Consultando ferramentas quando necessario.", 42)
                self._raise_if_canceled(job)
                tool_context = await self.tools.enrich(job.pedido, job.modo)
                self._raise_if_canceled(job)
                if tool_context.get("context"):
                    prompt = f"{prompt}\n\n[CONTEXTO DE FERRAMENTAS]\n{tool_context['context']}"

                await self._event_async(job, "Kemy", "Orquestrador ativo. Escolhendo o melhor motor gratuito.", 55)
                self._raise_if_canceled(job)
                result = await self.router.generate(
                    prompt,
                    job.modo,
                    attachments=attachment_context["items"],
                    visual_items=attachment_context["visual_items"],
                )
                self._raise_if_canceled(job)
                result = await self._maybe_self_review(job, prompt, result, execution_plan.as_prompt())
                self._raise_if_canceled(job)
                if job.modo == "coding" and self.settings.code_validation_enabled:
                    result = await self._validate_and_autofix_code(
                        job, prompt, result, execution_plan.as_prompt(), repository_context
                    )
                    self._raise_if_canceled(job)
                self._set_cached_response(cache_key, result)
            if job.modo == "site":
                result = self._ensure_site_artifact(job, result)
                if self.settings.code_validation_enabled:
                    result = await self._validate_and_autofix_code(
                        job, prompt, result, execution_plan.as_prompt(), repository_context=None
                    )
                    result = self._ensure_site_artifact(job, result)
            result = self._hydrate_artifacts(job, result)
            self._raise_if_canceled(job)
            result["pipeline"] = {
                "prompt_crafter": refined_prompt,
                "orchestrator_mode": job.modo,
                "system": "router-runtime",
                "execution_plan": execution_plan_data,
            }
            if job.github_repo:
                result["github_repo"] = job.github_repo
            result["execution_plan"] = execution_plan_data
            result["intent_validation"] = intent_validation.as_dict()
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
            self._raise_if_canceled(job)
            result.setdefault("security_report", "Nenhum segredo deve ser escrito no repositorio; use variaveis de ambiente.")

            await self._finish_job(job, result)
        except JobCanceledError as exc:
            await self._mark_job_canceled(job, str(exc) or "Execucao cancelada pelo usuario.")
        except Exception as exc:  # pragma: no cover - defensive runtime guard
            self._record_error_learning(job, exc)
            job.status = "error"
            job.etapa = "Erro na execucao"
            job.erro = str(exc)
            job.updated_at = utcnow()
            self.save(job)
            await self.supabase.insert_job(job.model_dump())

    async def _review_generated_image(self, job: JobState, result: dict[str, Any]) -> dict[str, Any]:
        """Uses a free visual model as art director when Gemini is configured."""
        if self.settings.llm_mode != "providers" or not self.settings.gemini_api_key:
            result["visual_qa"] = {
                "status": "local-only",
                "message": "Formato e integridade validados; critica semantica requer Gemini gratuito configurado.",
            }
            return result
        data_url = str(result.get("image_data_url") or "")
        if not data_url.startswith("data:image/") or "," not in data_url:
            return result
        try:
            header, encoded = data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded, validate=True)
            mime_type = header.split(";", 1)[0].replace("data:", "")
        except (ValueError, TypeError):
            return result
        critique_prompt = (
            "Voce e Kemy Art Director QA. Compare a imagem anexada com o pedido literal do usuario.\n"
            "Valide: assunto, quantidade de elementos, marca/texto exato, estilo, paleta, composicao, formato, "
            "legibilidade, anatomia, artefatos, recortes e ausencia de elementos aleatorios.\n"
            "Comece obrigatoriamente com APROVADO ou REPROVADO. Depois escreva no maximo 6 correcoes concretas. "
            "Nao elogie e nao exponha cadeia de pensamento.\n\n"
            f"Pedido: {job.pedido}\n"
            f"Brief usado: {result.get('enhanced_prompt', '')[:5000]}"
        )
        critique = await self.router.generate(
            critique_prompt,
            mode="auditoria",
            visual_items=[{"data": image_bytes, "mime_type": mime_type}],
        )
        text = str(critique.get("raw") or "").strip()
        approved = text.upper().startswith("APROVADO")
        result["visual_qa"] = {
            "status": "approved" if approved else "rejected",
            "critique": text[:2500],
            "provider": critique.get("provider"),
            "model": critique.get("model"),
        }
        if approved:
            return result
        repaired_prompt = (
            f"{job.pedido}. ART DIRECTOR CORRECTIONS (mandatory): {text[:1800]}. "
            "Preserve every original user requirement while fixing these issues."
        )
        regenerated = await self.pollinations.generate(repaired_prompt)
        regenerated["original_user_prompt"] = job.pedido
        regenerated["visual_qa"] = {
            "status": "regenerated-after-critique",
            "critique": text[:2500],
            "provider": critique.get("provider"),
            "model": critique.get("model"),
        }
        return regenerated

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
        if not reason and self.settings.deep_reasoning_enabled:
            reason = "revisao-deliberada-de-engenharia"
        if not reason:
            return result
        current = dict(result)
        current_raw = raw
        reviews: list[dict[str, Any]] = []
        passes = min(3, max(1, int(self.settings.reasoning_review_passes or 1)))
        if self.settings.adaptive_reasoning_budget:
            passes = reasoning_budget(job.pedido)
        draft_provider = str(result.get("provider") or "")
        for pass_number in range(1, passes + 1):
            review_prompt = self._build_self_review_prompt(
                job, prompt, current_raw, execution_plan_prompt, reason, pass_number=pass_number
            )
            try:
                reviewed = await self.router.generate(
                    review_prompt,
                    mode="auditoria",
                    exclude_providers={draft_provider} if draft_provider else None,
                )
                candidate = str(reviewed.get("raw") or reviewed.get("summary") or "").strip()
                accepted = bool(candidate) and self._is_review_candidate_better(job.modo, current_raw, candidate)
                reviews.append({
                    "pass": pass_number,
                    "accepted": accepted,
                    "provider": reviewed.get("provider"),
                    "model": reviewed.get("model"),
                })
                if not accepted:
                    break
                current_raw = candidate[: self.settings.self_review_max_chars]
                current["raw"] = current_raw
                current["summary"] = reviewed.get("summary") or current.get("summary") or "Resposta refinada automaticamente."
            except Exception as exc:
                reviews.append({"pass": pass_number, "accepted": False, "error": type(exc).__name__})
                break
        applied = any(item["accepted"] for item in reviews)
        current["qa_autofix"] = {"applied": applied, "reason": reason, "passes": reviews}
        if applied:
            current["tools_used"] = list(dict.fromkeys((result.get("tools_used") or []) + ["deep-reasoning-qa"]))
        return current

    async def _validate_and_autofix_code(
        self,
        job: JobState,
        prompt: str,
        result: dict[str, Any],
        execution_plan_prompt: str,
        repository_context: RepositoryContext | None,
    ) -> dict[str, Any]:
        current = dict(result)
        raw = str(current.get("raw") or "")
        existing_paths = set(repository_context.tree) if repository_context else None
        allow_new_files = any(
            marker in job.pedido.lower()
            for marker in ("crie", "criar", "adicione", "adicionar", "novo arquivo", "nova rota", "implemente")
        )
        require_tests = job.modo == "coding" and any(
            marker in job.pedido.lower()
            for marker in ("corrija", "conserte", "bug", "erro", "refatore", "implemente", "adicione")
        )
        design_request = job.pedido if job.modo == "site" or any(
            marker in job.pedido.lower()
            for marker in ("design", "interface", "frontend", "visual", "layout", "cor ", "tema")
        ) else ""
        base_root = repository_context.root if repository_context else None
        report = self.code_validator.validate(
            raw,
            existing_paths=existing_paths,
            allow_new_files=allow_new_files,
            base_root=base_root,
            require_tests=require_tests,
            design_request=design_request,
        )
        browser_report: BrowserReport | None = None
        if job.modo == "site" and report.ok and self.settings.browser_qa_enabled:
            browser_report = await self.frontend_browser.inspect(raw)
            visual_design_critique = await self._review_frontend_screenshot(job, browser_report)
            if visual_design_critique and visual_design_critique.upper().startswith("REPROVADO"):
                browser_report.ok = False
                browser_report.errors.append(f"Direcao de arte: {visual_design_critique[:1800]}")
            if browser_report.available and not browser_report.ok:
                report.ok = False
                report.errors.extend(browser_report.errors)
                report.checks.append({"name": "browser-runtime", "status": "failed", "detail": browser_report.as_prompt()[:1600]})
        attempts: list[dict[str, Any]] = [{"attempt": 0, **report.as_dict()}]
        max_attempts = min(3, max(0, int(self.settings.code_autofix_attempts or 0)))
        original_provider = str(result.get("provider") or "")
        for attempt in range(1, max_attempts + 1):
            if report.ok:
                break
            correction_prompt = (
                "Voce e Kemy Code Repair. Corrija a entrega usando os erros reais da validacao.\n"
                "Responda SOMENTE com um kemy_artifact completo. Cada arquivo alterado deve usar "
                "<file path=\"caminho/real\">conteudo completo</file>.\n"
                "Aplique patch incremental: preserve APIs, nomes e comportamento que nao precisam mudar.\n"
                "Inclua ou atualize testes especificos para o defeito. Nao use TODO, pseudocodigo ou dependencias pagas.\n"
                "Nao invente arquivos/simbolos ausentes sem declarar a criacao.\n\n"
                f"Esta e a tentativa {attempt}. Replaneje com base nas evidencias atuais. "
                "Se um erro se repetiu, descarte a hipotese anterior e escolha uma abordagem tecnicamente diferente; "
                "nao repita o mesmo patch com mais texto.\n"
                f"Modo da tarefa: {job.modo}. Para frontend, corrija tela vazia, assets, scripts e interacoes sem trocar o design pedido.\n\n"
                f"Pedido original:\n{job.pedido}\n\n"
                f"Plano:\n{execution_plan_prompt}\n\n"
                f"Erros da execucao/analise estatica:\n{report.as_prompt()}\n\n"
                f"Contexto relevante do repositorio:\n"
                f"{repository_context.as_prompt()[:14000] if repository_context else '- indisponivel'}\n\n"
                f"Entrega atual:\n{raw[:16000]}"
            )
            repaired = await self.router.generate(
                correction_prompt,
                mode="coding",
                exclude_providers={original_provider} if original_provider else None,
            )
            candidate = str(repaired.get("raw") or "")
            candidate_report = self.code_validator.validate(
                candidate,
                existing_paths=existing_paths,
                allow_new_files=allow_new_files,
                base_root=base_root,
                require_tests=require_tests,
                design_request=design_request,
            )
            candidate_browser: BrowserReport | None = None
            if job.modo == "site" and candidate_report.ok and self.settings.browser_qa_enabled:
                candidate_browser = await self.frontend_browser.inspect(candidate)
                candidate_critique = await self._review_frontend_screenshot(job, candidate_browser)
                if candidate_critique and candidate_critique.upper().startswith("REPROVADO"):
                    candidate_browser.ok = False
                    candidate_browser.errors.append(f"Direcao de arte: {candidate_critique[:1800]}")
                if candidate_browser.available and not candidate_browser.ok:
                    candidate_report.ok = False
                    candidate_report.errors.extend(candidate_browser.errors)
                    candidate_report.checks.append({
                        "name": "browser-runtime", "status": "failed",
                        "detail": candidate_browser.as_prompt()[:1600],
                    })
            attempts.append({"attempt": attempt, **candidate_report.as_dict(), "provider": repaired.get("provider"), "model": repaired.get("model")})
            if candidate_report.ok or len(candidate_report.errors) < len(report.errors):
                raw = candidate
                report = candidate_report
                browser_report = candidate_browser
                current["raw"] = raw
                current["summary"] = repaired.get("summary") or current.get("summary")
            else:
                break
        current["code_validation"] = {
            "ok": report.ok,
            "attempts": attempts,
            "sandbox": "isolated-project-copy-static-only",
        }
        browser_payload = browser_report.as_dict() if browser_report else {
            "available": False, "ok": False, "warnings": ["Browser QA nao aplicavel ou validacao estatica falhou antes da abertura."]
        }
        current["browser_validation"] = browser_payload
        sandbox_payload = {"available": False, "ok": False, "error": "Sandbox forte desativado ou validacao estatica pendente."}
        if self.settings.strong_sandbox_enabled and report.ok:
            sandbox_payload = await asyncio.to_thread(
                self.strong_sandbox.validate_artifact,
                raw,
                base_root,
                existing_paths,
            )
        current["sandbox_validation"] = sandbox_payload
        current["confidence"] = confidence_from_evidence(
            report.ok, browser=browser_payload, sandbox=sandbox_payload
        ).as_dict()
        current["agent_run"] = {
            "state": "completed" if report.ok else "needs_attention",
            "goal": job.pedido[:500],
            "iterations": len(attempts),
            "completion_criteria": [
                "arquivos pertencem ao workspace ou foram criados explicitamente",
                "sintaxe e formatos passam nos validadores locais",
                "nenhum caminho escapa da copia isolada",
                "testes acompanham alteracoes quando aplicavel",
            ],
            "evidence": {
                "validation_ok": report.ok,
                "checks": report.checks,
                "remaining_errors": report.errors,
                "repository_files_indexed": len(repository_context.tree) if repository_context else 0,
                "symbols_indexed": len(repository_context.symbols) if repository_context else 0,
            },
        }
        if repository_context:
            current["technical_memory"] = repository_context.technical_memory()
            project_id = hashlib.sha256(repository_context.root.encode("utf-8")).hexdigest()[:20]
            self.solution_history.record(project_id, {
                "fingerprint": hashlib.sha256(f"{job.pedido}|{raw}".encode("utf-8")).hexdigest()[:24],
                "request": job.pedido[:500],
                "success": report.ok,
                "errors": report.errors[:12],
                "checks": report.checks[:20],
                "provider": current.get("provider"),
                "model": current.get("model"),
                "confidence": current["confidence"],
            })
        current["tools_used"] = list(dict.fromkeys(
            (current.get("tools_used") or []) +
            ["repository-index", "safe-static-analysis"] +
            (["code-autofix"] if len(attempts) > 1 else [])
        ))
        current["evaluation"] = self.delivery_evaluator.evaluate(current)
        return current

    async def _review_frontend_screenshot(self, job: JobState, report: BrowserReport) -> str:
        if (
            not report.available
            or not report.screenshot_data_url
            or self.settings.llm_mode != "providers"
            or not self.settings.gemini_api_key
        ):
            return ""
        try:
            header, encoded = report.screenshot_data_url.split(",", 1)
            image_bytes = base64.b64decode(encoded, validate=True)
            mime_type = header.split(";", 1)[0].replace("data:", "")
        except (ValueError, TypeError):
            return ""
        prompt = (
            "Voce e Kemy Product Design QA. Compare o screenshot desktop com o pedido literal.\n"
            "Avalie aderencia ao segmento, marca, publico, cores, estilo, hierarquia, contraste, tipografia, grid, "
            "densidade, legibilidade, acabamento e identidade propria. Reprove templates genericos ou visual diferente "
            "do solicitado. Comece com APROVADO ou REPROVADO e liste no maximo 6 correcoes objetivas.\n\n"
            f"Pedido: {job.pedido}"
        )
        reviewed = await self.router.generate(
            prompt,
            mode="auditoria",
            visual_items=[{"data": image_bytes, "mime_type": mime_type}],
        )
        return str(reviewed.get("raw") or "").strip()

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
        pass_number: int = 1,
    ) -> str:
        base = (
            "Voce e Kemy Deep QA, um segundo engenheiro senior independente.\n"
            "Analise silenciosamente a solucao, encontre falhas reais e devolva somente a versao final corrigida.\n"
            "Use raciocinio interno estruturado: requisitos -> arquitetura -> corretude -> casos extremos -> seguranca -> testes.\n"
            "Nao exponha cadeia de pensamento. Mostre apenas decisoes tecnicas necessarias e a entrega final.\n"
            "Objetivo: preservar o pedido original, corrigir lacunas e tornar a entrega executavel.\n"
            "NUNCA devolva analise de auditoria, checklist interno, JSON cru ou metacomentario.\n"
        )
        if job.modo == "site":
            extra = (
                "Obrigatorio para site: responder com <kemy_artifact title=\"...\"> contendo pelo menos:\n"
                "1) <file path=\"preview.html\"> com HTML completo\n"
                "2) <file path=\"README.md\"> com instrucoes de uso\n"
                "Audite aderencia visual literal: nome/marca, segmento, publico, cores, estilo, secoes, componentes e funcionalidades pedidos.\n"
                "Audite qualidade de design: hierarquia, contraste, tipografia, espacamento, grid, responsividade, acessibilidade, estados e microinteracoes.\n"
                "Rejeite template generico, mesmo bonito, se nao representar exatamente o pedido do usuario.\n"
                "Sem texto fora do artifact.\n"
            )
        else:
            extra = (
                "Para coding: confira aderencia ao repositorio e a stack, imports, tipos, contratos, tratamento de erros, "
                "concorrencia, seguranca, compatibilidade e regressao. Preserve APIs existentes.\n"
                "Entregue diagnostico curto, arquivos afetados, patch/codigo completo e testes executaveis.\n"
                "Nao aceite placeholders, pseudocodigo, funcoes vazias, dependencias pagas obrigatorias ou desculpas vagas.\n"
            )
        return (
            f"{base}{extra}\n"
            f"Passo de revisao: {pass_number}\n"
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
        original_score = self._engineering_quality_score(mode, original)
        candidate_score = self._engineering_quality_score(mode, candidate)
        if candidate_score > original_score:
            return True
        if candidate_score == original_score and len(candidate.strip()) >= max(320, int(len(original.strip()) * 1.08)):
            return True
        return False

    def _engineering_quality_score(self, mode: str, text: str) -> int:
        """Cheap deterministic gate: a review must add engineering evidence, not verbosity."""
        lowered = text.lower()
        score = min(len(text) // 500, 6)
        signals = {
            "implementation": ["```", "<file path=", "diff --git", "arquivo", "files"],
            "verification": ["teste", "pytest", "npm test", "compileall", "como validar"],
            "correctness": ["erro", "excecao", "fallback", "edge case", "caso limite", "validacao"],
            "security": ["segur", "xss", "secret", "variavel de ambiente", "sanitize", "escape"],
            "compatibility": ["compatib", "preserv", "regress", "api existente", "contrato"],
        }
        for markers in signals.values():
            if any(marker in lowered for marker in markers):
                score += 2
        if mode == "site" and ("<kemy_artifact" in lowered or "<!doctype html" in lowered):
            score += 5
        penalties = ["todo", "fixme", "adicione aqui", "restante do codigo", "não posso", "nao posso"]
        score -= sum(2 for marker in penalties if marker in lowered)
        return score

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

    def _is_workspace_text_file(self, path: str, mime_type: str = "") -> bool:
        lowered = str(path or "").lower()
        if lowered.endswith(
            (
                ".py",
                ".js",
                ".ts",
                ".tsx",
                ".jsx",
                ".html",
                ".css",
                ".json",
                ".md",
                ".yml",
                ".yaml",
                ".sql",
                ".sh",
                ".env",
                ".toml",
                ".ini",
            )
        ):
            return True
        mime = str(mime_type or "").lower()
        return mime.startswith("text/") or mime in {
            "application/json",
            "application/javascript",
            "application/xml",
            "image/svg+xml",
        }

    def _workspace_file_rank(self, path: str) -> tuple[int, str]:
        lowered = str(path or "").lower()
        priority = [
            "preview.html",
            "index.html",
            "src/main.tsx",
            "src/app.tsx",
            "src/styles.css",
            "src/index.css",
            "app.py",
            "main.py",
            "readme.md",
            "package.json",
        ]
        for idx, key in enumerate(priority):
            if lowered == key:
                return idx, lowered
        return len(priority) + 1, lowered

    def _extract_workspace_files_from_history_item(self, item: dict[str, Any]) -> list[dict[str, str]]:
        direct_snapshot = item.get("workspace_snapshot") or {}
        if isinstance(direct_snapshot, dict):
            snapshot_files = direct_snapshot.get("files")
            if isinstance(snapshot_files, list) and snapshot_files:
                normalized_direct: list[dict[str, str]] = []
                for file_item in snapshot_files[:14]:
                    if not isinstance(file_item, dict):
                        continue
                    path = str(file_item.get("path") or file_item.get("relative_path") or file_item.get("name") or "").replace("\\", "/").strip().lstrip("/")
                    content = str(file_item.get("content") or "")
                    if not path or not content.strip():
                        continue
                    cleaned = self._clean_artifact_content(path, content).strip()
                    if not cleaned:
                        continue
                    normalized_direct.append({"path": path, "content": cleaned[:9000]})
                if normalized_direct:
                    normalized_direct.sort(key=lambda row: self._workspace_file_rank(str(row.get("path") or "")))
                    return normalized_direct

        result = item.get("result") or {}
        candidates: list[list[dict[str, Any]]] = []
        site_snapshot = item.get("site_snapshot") or result.get("site_snapshot") or {}
        if isinstance(site_snapshot, dict):
            site_files = site_snapshot.get("files")
            if isinstance(site_files, list):
                candidates.append(
                    [
                        {
                            "relative_path": row.get("path"),
                            "content": row.get("content"),
                            "mime_type": "text/html" if str(row.get("path") or "").lower().endswith(".html") else "text/plain",
                        }
                        for row in site_files
                        if isinstance(row, dict)
                    ]
                )
        for key in ("files",):
            values = item.get(key)
            if isinstance(values, list):
                candidates.append(values)
            result_values = result.get(key)
            if isinstance(result_values, list):
                candidates.append(result_values)

        merged: dict[str, dict[str, str]] = {}
        budget = 44000
        for file_list in candidates:
            for file_item in file_list:
                if not isinstance(file_item, dict):
                    continue
                path = str(file_item.get("relative_path") or file_item.get("path") or file_item.get("name") or "").replace("\\", "/").strip().lstrip("/")
                if not path:
                    continue
                mime_type = str(file_item.get("mime_type") or "")
                if not self._is_workspace_text_file(path, mime_type):
                    continue
                content = str(file_item.get("content") or "")
                if not content.strip():
                    continue
                cleaned = self._clean_artifact_content(path, content).strip()
                if not cleaned:
                    continue
                per_file_limit = 12000 if path.lower().endswith(".html") else 8000
                clipped = cleaned[: min(per_file_limit, budget)]
                if not clipped:
                    continue
                merged[path] = {"path": path, "content": clipped}
                budget -= len(clipped)
                if budget <= 2400:
                    break
            if budget <= 2400:
                break
        if not merged:
            return []
        ordered = sorted(merged.values(), key=lambda row: self._workspace_file_rank(str(row.get("path") or "")))
        return ordered[:14]

    def _workspace_snapshot_from_result(self, result: dict[str, Any]) -> dict[str, Any] | None:
        files_raw = result.get("files")
        if not isinstance(files_raw, list):
            return None
        history_like = {
            "files": files_raw,
            "result": {"files": files_raw, "site_snapshot": result.get("site_snapshot")},
        }
        files = self._extract_workspace_files_from_history_item(history_like)
        if not files:
            return None
        title = str(result.get("artifact_title") or result.get("document_title") or result.get("summary") or "Workspace Kemy")
        summary = str(result.get("summary") or "")[:220]
        return {"title": title[:90], "summary": summary, "files": files}

    def _latest_workspace_snapshot(self, session_data: dict[str, Any]) -> dict[str, Any] | None:
        history = (session_data or {}).get("historico") or []
        for item in reversed(history[-24:]):
            if item.get("role") != "assistant":
                continue
            candidate = item.get("workspace_snapshot")
            if isinstance(candidate, dict) and candidate.get("files"):
                return candidate
            result = item.get("result") or {}
            result_candidate = result.get("workspace_snapshot")
            if isinstance(result_candidate, dict) and result_candidate.get("files"):
                return result_candidate
            files = self._extract_workspace_files_from_history_item(item)
            if files:
                title = (
                    str(result.get("artifact_title") or "")
                    or str(result.get("document_title") or "")
                    or str(result.get("summary") or "")
                    or "Workspace Kemy"
                )
                return {"title": title[:90], "summary": str(item.get("content") or "")[:220], "files": files}
        return None

    def _build_workspace_edit_context(self, job: JobState, session_data: dict[str, Any]) -> str:
        snapshot = self._latest_workspace_snapshot(session_data)
        if not snapshot:
            return ""
        files = snapshot.get("files") or []
        if not files:
            return ""
        budget = 42000
        blocks: list[str] = []
        for file_item in files[:12]:
            path = str(file_item.get("path") or "").replace("\\", "/").strip()
            content = str(file_item.get("content") or "")
            if not path or not content:
                continue
            cleaned = self._clean_artifact_content(path, content).strip()
            if not cleaned:
                continue
            limit = 11000 if path.lower().endswith(".html") else 7600
            clipped = cleaned[: min(limit, budget)]
            if not clipped:
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
            if budget <= 2000:
                break
        if not blocks:
            return ""
        title = str(snapshot.get("title") or "Workspace Kemy").strip()
        summary = str(snapshot.get("summary") or "").strip()
        summary_line = f"Resumo atual: {summary}\n" if summary else ""
        return (
            f"Workspace atual: {title}\n"
            f"{summary_line}"
            "Contexto: existe um projeto de codigo em andamento nesta sessao.\n"
            "A tarefa atual deve continuar no mesmo projeto, preservando o que funciona.\n"
            "Edite os arquivos necessarios sem reiniciar do zero.\n\n"
            "\n\n".join(blocks)
        )

    def _is_workspace_edit_request(self, pedido: str, session_data: dict[str, Any]) -> bool:
        text = " ".join((pedido or "").split()).lower()
        if not text:
            return False
        new_project_signals = [
            "novo projeto",
            "do zero",
            "from scratch",
            "reiniciar",
            "recomecar",
            "comecar do zero",
            "crie um novo projeto",
            "gere um novo projeto",
            "novo app",
            "novo site",
            "iniciar outro projeto",
        ]
        if any(sig in text for sig in new_project_signals):
            return False
        edit_signals = [
            "arruma",
            "arrume",
            "conserta",
            "conserte",
            "muda",
            "mude",
            "altera",
            "altere",
            "ajusta",
            "ajuste",
            "corrige",
            "corrija",
            "refatora",
            "refatore",
            "melhora",
            "melhore",
            "adiciona",
            "adicione",
            "implemente",
            "otimiza",
            "otimize",
            "nesse codigo",
            "neste codigo",
            "nesse projeto",
            "neste projeto",
            "continue",
            "continua",
            "atualize",
        ]
        if any(sig in text for sig in edit_signals):
            return True
        has_workspace = bool(self._latest_workspace_snapshot(session_data))
        if not has_workspace:
            return False
        if len(text.split()) <= 28:
            return True
        contextual_refs = ["isso", "aqui", "desse jeito", "dessa forma", "mesmo estilo"]
        return any(sig in text for sig in contextual_refs)

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

    def _build_error_learning_context(self, session_data: dict[str, Any] | None) -> str:
        lessons = (session_data or {}).get("error_memory") or []
        if not isinstance(lessons, list) or not lessons:
            return ""
        lines: list[str] = []
        for item in lessons[-6:]:
            if not isinstance(item, dict):
                continue
            mode = str(item.get("mode") or "auto")
            pedido = str(item.get("pedido") or "").strip()
            error = str(item.get("error") or item.get("lesson") or "").strip()
            if not error:
                continue
            lesson = self._lesson_from_error(error, mode, pedido)
            lines.append(f"- [{mode}] {lesson}")
        if not lines:
            return ""
        return (
            "## Aprendizados de erros recentes desta conversa\n"
            "Use estes pontos como protecoes, sem mencionar ao usuario se nao for necessario:\n"
            f"{chr(10).join(lines)}\n"
        )

    def _lesson_from_error(self, error: str, mode: str, pedido: str = "") -> str:
        lowered = f"{error} {pedido}".lower()
        if "document" in lowered and "site" in lowered:
            return "nao misturar documento com site; respeitar o modo validado antes de gerar arquivo"
        if "preview" in lowered or "iframe" in lowered or "html" in lowered:
            return "para site, entregar preview.html renderizavel e manter arquivos do projeto acessiveis"
        if "nao autenticado" in lowered or "não autenticado" in lowered:
            return "validar sessao/autenticacao antes de aplicar alteracoes no preview"
        if "missing" in lowered and "argument" in lowered:
            return "checar assinatura das funcoes antes de chamar geradores de documento/slide"
        if "codigo" in lowered or "code" in lowered or "repo" in lowered or "git" in lowered:
            return "quando houver workspace/repositorio, ler contexto e editar codigo existente em vez de criar template generico"
        clipped = re.sub(r"\s+", " ", error).strip()[:180]
        return clipped or "revalidar pedido, modo e entregavel antes de responder"

    def _record_error_learning(self, job: JobState, exc: Exception | str, lesson: str | None = None) -> None:
        try:
            key = f"session:{job.session_id}"
            now = utcnow()
            data = self.storage.get_json(key, {"session_id": job.session_id, "historico": [], "created_at": now})
            errors = data.setdefault("error_memory", [])
            if not isinstance(errors, list):
                errors = []
            error_text = str(exc)
            entry = {
                "ts": now,
                "job_id": job.job_id,
                "mode": job.modo,
                "pedido": job.pedido[:220],
                "error": error_text[:600],
                "lesson": lesson or self._lesson_from_error(error_text, job.modo, job.pedido),
            }
            errors.append(entry)
            data["error_memory"] = errors[-12:]
            data["updated_at"] = now
            self.storage.set_json(key, data, ttl=self.settings.session_ttl_seconds)
        except Exception:
            return

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
        if parsed and self._artifact_has_usable_html(parsed.files):
            return result
        if result.get("files") and self._result_files_have_usable_html(result):
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
        result["raw"] = self._fallback_site_artifact_v2(job.pedido)
        result["summary"] = "Site gerado com live preview, arquivos e ZIP para download."
        tools = list(result.get("tools_used") or [])
        if "kemy-site-fallback" not in tools:
            tools.append("kemy-site-fallback")
        result["tools_used"] = tools
        self._record_error_learning(
            job,
            "site result without usable preview.html",
            lesson="site precisa entregar preview.html real; nao aceitar doc/pdf como resultado principal de site",
        )
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

    def _result_files_have_usable_html(self, result: dict[str, Any]) -> bool:
        if result.get("preview_url") or result.get("site_url"):
            return True
        for file_item in result.get("files") or []:
            if not isinstance(file_item, dict):
                continue
            path = str(file_item.get("name") or file_item.get("path") or file_item.get("relative_path") or "").lower()
            if not path.endswith(".html"):
                continue
            content = self._clean_artifact_content(path, str(file_item.get("content") or ""))
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

    def _fallback_site_artifact_v2(self, pedido: str) -> str:
        title = self._site_title_from_prompt(pedido)
        normalized_prompt = " ".join(pedido.split())
        lowered = normalized_prompt.lower()
        safe_title = html.escape(title)
        safe_prompt = html.escape(normalized_prompt)
        premium_dark = any(token in lowered for token in ["dourado", "preto", "premium", "luxo", "black"])
        include_prices = any(token in lowered for token in ["preco", "precos", "valor", "valores", "tabela"])
        include_gallery = any(token in lowered for token in ["imagem", "imagens", "foto", "fotos", "corte", "cortes", "galeria"])
        gallery_sources = [
            "https://source.unsplash.com/1200x900/?barber,shop",
            "https://source.unsplash.com/1200x900/?haircut,barber",
            "https://source.unsplash.com/1200x900/?beard,barbershop",
        ]
        services = [
            ("Corte Classico", "Acabamento alinhado com navalha e finalizacao premium.", "R$ 45"),
            ("Degrade Premium", "Tecnica moderna com transicao limpa e styling.", "R$ 60"),
            ("Barba Completa", "Toalha quente, desenho e hidratacao da pele.", "R$ 35"),
            ("Combo Corte + Barba", "Pacote executivo para visual completo.", "R$ 85"),
        ]
        service_cards = []
        for name, desc, price in services:
            price_html = f"<p class=\"price\">{html.escape(price)}</p>" if include_prices else ""
            service_cards.append(
                f"<article class=\"service-card\">"
                f"<h3>{html.escape(name)}</h3>"
                f"<p>{html.escape(desc)}</p>"
                f"{price_html}"
                "</article>"
            )
        gallery_html = ""
        if include_gallery:
            gallery_items = "".join(
                f"<figure><img src=\"{src}\" alt=\"Corte de cabelo\" loading=\"lazy\"/></figure>"
                for src in gallery_sources
            )
            gallery_html = (
                "<section class=\"gallery\" id=\"galeria\">"
                "<div class=\"section-head\"><span>Galeria</span><h2>Cortes em destaque</h2></div>"
                f"<div class=\"gallery-grid\">{gallery_items}</div>"
                "</section>"
            )
        palette_css = (
            ":root{--bg:#050708;--surface:#101316;--surface-2:#191d21;--text:#f6f6f0;--muted:#c4c7cc;--brand:#d6b16d;--line:rgba(214,177,109,.28);--button:#d6b16d;--button-text:#0b0d0f;}"
            if premium_dark
            else ":root{--bg:#f3f8f6;--surface:#ffffff;--surface-2:#eef4f1;--text:#10231d;--muted:#5f756e;--brand:#128d72;--line:rgba(16,35,29,.14);--button:#10231d;--button-text:#ffffff;}"
        )
        subtitle = (
            "Barbearia premium com atendimento por agenda, visual marcante e experiencia focada no cliente."
            if premium_dark
            else "Atendimento moderno para clientes agendarem online com rapidez e conforto."
        )
        html_doc = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>
    {palette_css}
    *{{box-sizing:border-box}}
    body{{margin:0;background:var(--bg);color:var(--text);font-family:Inter,system-ui,sans-serif}}
    .wrap{{width:min(1180px,calc(100% - 36px));margin:0 auto}}
    header{{padding:26px 0;display:flex;justify-content:space-between;align-items:center;gap:14px}}
    .logo{{font-size:30px;font-weight:900;letter-spacing:-.04em}}
    nav{{display:flex;gap:18px;color:var(--muted);font-weight:700}}
    .hero{{display:grid;grid-template-columns:1.1fr .9fr;gap:20px;margin-bottom:18px}}
    .panel{{border:1px solid var(--line);border-radius:28px;background:var(--surface);padding:28px;box-shadow:0 14px 42px rgba(0,0,0,.12)}}
    .eyebrow{{display:inline-flex;padding:7px 12px;border-radius:999px;background:color-mix(in srgb,var(--brand),transparent 84%);color:var(--brand);font-size:12px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}}
    h1{{margin:18px 0 12px;font-size:clamp(36px,6vw,68px);line-height:.96;letter-spacing:-.06em}}
    .lead{{margin:0;color:var(--muted);font-size:18px;line-height:1.5}}
    .actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}}
    .btn{{border:0;border-radius:999px;padding:14px 20px;font-weight:800;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}}
    .btn-primary{{background:var(--button);color:var(--button-text)}}
    .btn-secondary{{background:transparent;color:var(--text);border:1px solid var(--line)}}
    .booking h2{{margin:0 0 12px;font-size:26px;letter-spacing:-.03em}}
    .grid{{display:grid;gap:10px}}
    label{{display:grid;gap:6px;font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}
    input,select{{width:100%;padding:13px 14px;border-radius:14px;border:1px solid var(--line);background:var(--surface-2);color:var(--text);font:inherit}}
    .services{{margin:16px 0 22px;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}}
    .service-card{{border:1px solid var(--line);border-radius:20px;background:var(--surface);padding:18px}}
    .service-card h3{{margin:0 0 7px;font-size:21px;letter-spacing:-.03em}}
    .service-card p{{margin:0;color:var(--muted);line-height:1.48}}
    .price{{margin-top:12px!important;font-size:18px!important;font-weight:800;color:var(--brand)!important}}
    .section-head span{{color:var(--brand);font-size:12px;font-weight:800;text-transform:uppercase;letter-spacing:.1em}}
    .section-head h2{{margin:6px 0 0;font-size:34px;letter-spacing:-.04em}}
    .gallery{{margin:14px 0 40px}}
    .gallery-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}}
    .gallery-grid figure{{margin:0;border-radius:18px;overflow:hidden;border:1px solid var(--line);background:var(--surface)}}
    .gallery-grid img{{width:100%;height:230px;object-fit:cover;display:block}}
    .request-note{{margin:0 0 24px;color:var(--muted);font-size:13px}}
    @media (max-width:900px){{
      nav{{display:none}}
      .hero{{grid-template-columns:1fr}}
      .gallery-grid{{grid-template-columns:1fr}}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div class="logo">{safe_title}</div>
      <nav><span>Servicos</span><span>Agenda</span><span>Galeria</span><span>Contato</span></nav>
    </header>
    <section class="hero">
      <article class="panel">
        <span class="eyebrow">Agenda online</span>
        <h1>{safe_title}</h1>
        <p class="lead">{html.escape(subtitle)}</p>
        <div class="actions">
          <a class="btn btn-primary" href="#agendar">Agendar agora</a>
          <a class="btn btn-secondary" href="#servicos">Ver servicos</a>
        </div>
      </article>
      <form id="agendar" class="panel booking">
        <h2>Reserve seu horario</h2>
        <div class="grid">
          <label>Nome<input placeholder="Seu nome"/></label>
          <label>Servico<select>{''.join(f'<option>{html.escape(item[0])}</option>' for item in services[:4])}</select></label>
          <label>Data<input type="date"/></label>
          <label>Horario<input type="time"/></label>
        </div>
        <button class="btn btn-primary" type="button" onclick="alert('Agendamento simulado com sucesso!')">Confirmar agendamento</button>
      </form>
    </section>
    <section id="servicos">
      <div class="section-head"><span>Servicos</span><h2>Catalogo de cortes</h2></div>
      <div class="services">{''.join(service_cards)}</div>
    </section>
    {gallery_html}
    <p class="request-note"><strong>Pedido aplicado:</strong> {safe_prompt}</p>
  </div>
</body>
</html>"""
        readme = f"""# {title}

Site de contingencia personalizado pela Kemy para: {pedido}

## O que foi aplicado automaticamente

- Nome do projeto: `{title}`
- {"Tabela de precos ficticios incluida." if include_prices else "Estrutura de servicos pronta para receber precos."}
- {"Galeria de imagens de cortes incluida no preview." if include_gallery else "Sem galeria automatica porque o pedido nao exigiu imagens."}
- {"Paleta premium escura (dourado + preto) aplicada." if premium_dark else "Paleta clara profissional aplicada."}

## Como usar

- Abra `preview.html` para visualizar imediatamente.
- Se quiser ajustes de marca (cores, nome, precos, imagens), peca no campo de edicao do preview.
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
        explicit_name = self._extract_brand_name_from_prompt(text)
        if explicit_name:
            return explicit_name
        if "barbe" in lowered or "corte" in lowered or "cabelo" in lowered:
            return "Studio de Cortes Premium"
        if "imobili" in lowered:
            return "Imobiliaria Prime"
        if "restaurante" in lowered or "comida" in lowered:
            return "Mesa Reservada"
        return (text[:48].strip() or "Site Kemy").title()

    def _extract_brand_name_from_prompt(self, text: str) -> str:
        source = " ".join((text or "").split())
        patterns = [
            r"(?:com\s+nome|nome\s*[:\-]|chamado|chamada)\s+([A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ '&-]{2,42})",
            r"(?:marca|empresa)\s*[:\-]\s*([A-Za-z0-9À-ÿ][A-Za-z0-9À-ÿ '&-]{2,42})",
        ]
        for pattern in patterns:
            match = re.search(pattern, source, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = re.split(r"(?:\s+(?:com|e|para|no|na|de|do|da)\s+.*)$", match.group(1).strip(), maxsplit=1, flags=re.IGNORECASE)[0]
            cleaned = re.sub(r"\s{2,}", " ", candidate).strip(" .,:;!-")
            if len(cleaned) >= 3:
                return cleaned[:52]
        return ""

    def _repo_display_name(self, repo_url: str) -> str:
        value = str(repo_url or "").strip().rstrip("/")
        if not value:
            return "repositorio"
        if value.endswith(".git"):
            value = value[:-4]
        if "github.com/" in value:
            return value.split("github.com/", 1)[1]
        return value.split("/")[-1] or value

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

    def _build_reasoning_trace(self, pedido: str, mode: str) -> str:
        text = " ".join((pedido or "").split()).lower()
        requirements: list[str] = []
        if mode == "site":
            if "nome" in text:
                requirements.append("usar nome de marca solicitado")
            if "preco" in text or "valor" in text:
                requirements.append("incluir tabela/lista de precos")
            if "imagem" in text or "foto" in text:
                requirements.append("incluir galeria de imagens contextual")
            if any(token in text for token in ["dourado", "preto", "premium", "luxo"]):
                requirements.append("aplicar direcao visual premium solicitada")
            if "agend" in text:
                requirements.append("manter fluxo de agendamento funcional")
        if mode == "coding":
            if any(token in text for token in ["arrume", "corrija", "conserte", "refatore", "ajuste", "bug", "erro"]):
                requirements.append("corrigir codigo existente sem criar template novo")
            if any(token in text for token in ["workspace", "repositorio", "repo", "github", "git"]):
                requirements.append("usar workspace/repositorio ativo da sessao")
        if mode == "documento" and any(token in text for token in ["abnt", "docx", "pdf", "relatorio"]):
            requirements.append("entregar documento estruturado sem codigo tecnico")
        if not requirements:
            return ""
        return "; ".join(requirements[:4]) + "."

    async def _mark_job_canceled(self, job: JobState, reason: str = "Execucao cancelada pelo usuario.") -> None:
        reason = (reason or "Execucao cancelada pelo usuario.").strip()
        if not any(str(event.get("msg") or "").lower() == reason.lower() for event in job.eventos):
            job.eventos.append(
                {
                    "ts": utcnow(),
                    "agente": "Kemy",
                    "msg": reason,
                    "progresso": min(max(job.progresso, 1), 98),
                }
            )
        job.status = "canceled"
        job.etapa = "Cancelado pelo usuario"
        job.erro = reason
        job.updated_at = utcnow()
        self.save(job)
        await self.supabase.insert_job(job.model_dump())

    def _event(self, job: JobState, agente: str, msg: str, progresso: int) -> None:
        if self._is_cancel_requested(job.job_id):
            return
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

    def _infer_preview_url_from_files(self, files: list[dict[str, Any]]) -> str:
        for item in files or []:
            if not isinstance(item, dict):
                continue
            download = str(item.get("download_url") or "")
            if not download:
                continue
            path = str(item.get("relative_path") or item.get("path") or item.get("name") or "").lower()
            mime_type = str(item.get("mime_type") or "").lower()
            if path.endswith(".html") or mime_type == "text/html":
                return download
        return ""

    def _compact_result_metadata(self, result: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
        files = self._compact_files(result.get("files") or [])
        preview_url = str(result.get("preview_url") or self._infer_preview_url_from_files(result.get("files") or []))
        metadata = {
            "summary": result.get("summary"),
            "document_title": result.get("document_title"),
            "artifact_title": result.get("artifact_title"),
            "provider": result.get("provider"),
            "model": result.get("model"),
            "tools_used": list(result.get("tools_used") or []),
            "preview_url": preview_url,
            "project_archive_url": result.get("project_archive_url"),
            "image_url": result.get("image_url"),
            "github_repo": result.get("github_repo"),
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
        if isinstance(result.get("code_validation"), dict):
            validation = result["code_validation"]
            metadata["code_validation"] = {
                "ok": validation.get("ok"),
                "sandbox": validation.get("sandbox"),
                "attempt_count": len(validation.get("attempts") or []),
            }
        if isinstance(result.get("agent_run"), dict):
            run = result["agent_run"]
            metadata["agent_run"] = {
                "state": run.get("state"),
                "iterations": run.get("iterations"),
                "completion_criteria": run.get("completion_criteria"),
                "evidence": run.get("evidence"),
            }
        if isinstance(result.get("visual_qa"), dict):
            visual_qa = result["visual_qa"]
            metadata["visual_qa"] = {
                "status": visual_qa.get("status"),
                "provider": visual_qa.get("provider"),
                "model": visual_qa.get("model"),
                "critique": str(visual_qa.get("critique") or "")[:1200],
            }
        if isinstance(result.get("image_validation"), dict):
            metadata["image_validation"] = result["image_validation"]
        if isinstance(result.get("browser_validation"), dict):
            browser = result["browser_validation"]
            metadata["browser_validation"] = {
                "available": browser.get("available"),
                "ok": browser.get("ok"),
                "errors": list(browser.get("errors") or [])[:12],
                "warnings": list(browser.get("warnings") or [])[:12],
                "viewports": list(browser.get("viewports") or [])[:4],
            }
        if isinstance(result.get("confidence"), dict):
            metadata["confidence"] = result["confidence"]
        if isinstance(result.get("sandbox_validation"), dict):
            sandbox = result["sandbox_validation"]
            metadata["sandbox_validation"] = {
                "available": sandbox.get("available"),
                "ok": sandbox.get("ok"),
                "command": sandbox.get("command"),
                "error": sandbox.get("error"),
                "returncode": sandbox.get("returncode"),
            }
        if isinstance(result.get("evaluation"), dict):
            metadata["evaluation"] = result["evaluation"]
        workspace_snapshot = result.get("workspace_snapshot")
        if not isinstance(workspace_snapshot, dict):
            workspace_snapshot = self._workspace_snapshot_from_result(result)
        if isinstance(workspace_snapshot, dict) and workspace_snapshot.get("files"):
            metadata["workspace_snapshot"] = workspace_snapshot
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
        workspace_snapshot = self._workspace_snapshot_from_result(result)
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
            "github_repo": result.get("github_repo"),
            "files": public_files,
            "preview_url": result.get("preview_url") or self._infer_preview_url_from_files(result.get("files") or []),
            "result": result_metadata,
        }
        if site_snapshot:
            assistant_entry["site_snapshot"] = site_snapshot
            assistant_entry["result"]["site_snapshot"] = site_snapshot
        if workspace_snapshot:
            assistant_entry["workspace_snapshot"] = workspace_snapshot
            assistant_entry["result"]["workspace_snapshot"] = workspace_snapshot
        if isinstance(result.get("technical_memory"), dict):
            data["technical_memory"] = result["technical_memory"]
            assistant_entry["technical_memory"] = result["technical_memory"]
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
        if self._is_cancel_requested(job.job_id):
            await self._mark_job_canceled(job, "Execucao cancelada pelo usuario.")
            return
        result = dict(result or {})
        result.setdefault("mode", job.modo)
        if job.github_repo and not result.get("github_repo"):
            result["github_repo"] = job.github_repo
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
        if assistant_message.get("workspace_snapshot"):
            assistant_metadata["workspace_snapshot"] = assistant_message.get("workspace_snapshot")
        await self.supabase.insert_message(
            job.session_id,
            "assistant",
            self._display_answer_for_history(result),
            assistant_metadata,
        )
        for file_item in result.get("files") or []:
            await self.supabase.insert_generated_file(job.job_id, file_item)
