from __future__ import annotations

import asyncio
import html
import json
import re
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
            if attachment_context.get("prompt_context"):
                prompt = f"{prompt}\n\n[ANEXOS PROCESSADOS]\n{attachment_context['prompt_context']}"

            if job.modo == "imagem":
                await self._event_async(job, "Kemy", "Pedido visual detectado. Vou gerar a imagem na rota apropriada.", 48)
                result = await self.pollinations.generate(job.pedido)
                result["tools_used"] = ["pollinations"]
                result["execution_plan"] = execution_plan.as_dict()
                await self._finish_job(job, result)
                return

            if job.modo == "documento":
                await self._event_async(job, "Kemy", "Orquestrador ativo. Passando o trabalho para o sistema de documentos.", 45)
                draft = await self.router.generate(
                    prompt,
                    job.modo,
                    attachments=attachment_context["items"],
                    visual_items=attachment_context["visual_items"],
                )
                await self._event_async(job, "Kemy", "Montando arquivos finais do documento.", 72)
                result = self.documents.generate(job.session_id, job.job_id, job.pedido, draft)
                result["tools_used"] = list(
                    dict.fromkeys((draft.get("tools_used") or []) + (result.get("tools_used") or []) + ["python-docx", "reportlab"])
                )
                result["pipeline"] = {
                    "prompt_crafter": refined_prompt,
                    "orchestrator_mode": job.modo,
                    "system": "document-service",
                    "execution_plan": execution_plan.as_dict(),
                }
                result["execution_plan"] = execution_plan.as_dict()
                await self._finish_job(job, result)
                return

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
            if job.modo == "site":
                result = self._ensure_site_artifact(job, result)
            result = self._hydrate_artifacts(job, result)
            result["pipeline"] = {
                "prompt_crafter": refined_prompt,
                "orchestrator_mode": job.modo,
                "system": "router-runtime",
                "execution_plan": execution_plan.as_dict(),
            }
            result["execution_plan"] = execution_plan.as_dict()
            result["tools_used"] = tool_context.get("used", [])
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
            content = self._clean_artifact_content(original_path, artifact.content)
            safe_name = original_path.replace("/", "__") or "index.html"
            target = folder / safe_name
            target.write_text(content, encoding="utf-8")
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
        if html_candidate and self._looks_like_html_document(html_candidate):
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
            if "<html" in content.lower() or "<!doctype html" in content.lower():
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
        for file_item in result.get("files") or []:
            await self.supabase.insert_generated_file(job.job_id, file_item)
