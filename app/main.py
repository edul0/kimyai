from __future__ import annotations

import base64
import re
import shutil
import uuid
from mimetypes import guess_type
from pathlib import Path

import yaml
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .agents import DEFAULT_AGENTS
from .auth import COOKIE_NAME, MAX_AGE, create_token, create_user, find_user, token_subject, verify_password, verify_token
from .config import get_settings
from .jobs import JobManager, utcnow
from .schemas import ComandoRequest, JobCreateResponse, JobState, NovoAgente, SitePublishRequest, SitePublishResponse
from .storage import Storage
from .supabase_store import SupabaseStore
from .github_service import GitHubService

settings = get_settings()
storage = Storage(settings.redis_url)
jobs = JobManager(storage, settings)
supabase_auth = SupabaseStore(settings)
github = GitHubService(
    token=settings.github_token,
    default_repo=settings.github_repo_url,
    default_branch=settings.github_default_branch,
    user_name=settings.github_user_name,
    user_email=settings.github_user_email,
)

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="Kemy AI cloud-free multi-agent coding workspace.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials="*" not in settings.cors_list,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_login(request: Request, call_next):
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    base_domain = str(settings.site_base_domain or "").strip().lower().strip("/")
    if base_domain and host.endswith(f".{base_domain}"):
        slug = host[: -(len(base_domain) + 1)].strip(".")
        if slug:
            asset_path = request.url.path.lstrip("/")
            try:
                return _published_file_response(slug, asset_path)
            except HTTPException:
                pass
    public_paths = (
        "/static/", "/api/auth/", "/docs", "/redoc", "/openapi.json",
        "/api/status", "/favicon.ico",
        "/api/github/connect", "/api/github/callback", "/api/github/me",
    )
    path = request.url.path
    if path.startswith(public_paths) or path in {"/"}:
        return await call_next(request)
    if path.startswith("/api/") and not verify_token(request.cookies.get(COOKIE_NAME), settings):
        return JSONResponse({"detail": "Nao autenticado."}, status_code=401)
    return await call_next(request)

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

published_sites_root = Path("generated_documents") / "published_sites"
published_sites_root.mkdir(parents=True, exist_ok=True)


def _normalize_public_prefix(value: str) -> str:
    cleaned = str(value or "/p").strip()
    if not cleaned.startswith("/"):
        cleaned = f"/{cleaned}"
    cleaned = cleaned.rstrip("/") or "/p"
    return cleaned


def _sanitize_slug(value: str, fallback: str = "site-kemy") -> str:
    base = (value or "").strip().lower()
    base = re.sub(r"[^a-z0-9-]+", "-", base)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    if not base:
        base = fallback
    if not re.match(r"^[a-z0-9]", base):
        base = f"s-{base}"
    return base[:60]


def _allocate_site_slug(base_slug: str) -> str:
    slug = _sanitize_slug(base_slug, fallback="site-kemy")
    if not (published_sites_root / slug).exists():
        return slug
    for index in range(2, 1000):
        candidate = f"{slug}-{index}"
        if not (published_sites_root / candidate).exists():
            return candidate
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _site_subdomain_url(slug: str) -> str | None:
    domain = str(settings.site_base_domain or "").strip().strip("/")
    if not domain:
        return None
    if domain.startswith("http://") or domain.startswith("https://"):
        domain = domain.split("://", 1)[1]
    return f"https://{slug}.{domain}"


SITE_PUBLIC_PREFIX = _normalize_public_prefix(settings.site_public_prefix)


def _cache_session_payload(payload: dict) -> dict:
    session_id = payload.get("session_id") or payload.get("id")
    data = {
        "session_id": session_id,
        "owner": payload.get("owner") or payload.get("owner_email"),
        "title": payload.get("title") or "Nova conversa",
        "historico": payload.get("historico", []),
        "memoria": payload.get("memoria", []),
        "contexto_compacto": payload.get("contexto_compacto") or {},
        "last_mode": payload.get("last_mode"),
        "last_github_repo": payload.get("last_github_repo"),
        "created_at": payload.get("created_at") or utcnow(),
        "updated_at": payload.get("updated_at") or payload.get("created_at") or utcnow(),
    }
    storage.set_json(f"session:{session_id}", data, ttl=settings.session_ttl_seconds)
    return data


def _preview_from_history(items: list[dict]) -> str:
    for item in reversed(items):
        result = item.get("result") or {}
        preview = (
            result.get("document_title")
            or result.get("summary")
            or item.get("content")
            or item.get("usuario")
            or item.get("resumo")
            or ""
        )
        preview = " ".join(str(preview).split()).strip()
        if preview:
            return preview[:90]
    return ""


def _local_session_jobs(session_id: str) -> list[dict]:
    items = []
    for key in storage.keys("job:"):
        data = storage.get_json(key)
        if data and data.get("session_id") == session_id:
            items.append(data)
    return sorted(items, key=lambda item: item.get("created_at", ""))


async def _session_jobs(session_id: str) -> list[dict]:
    items_by_id = {item.get("job_id"): item for item in _local_session_jobs(session_id)}
    for row in await jobs.supabase.list_jobs_for_session(session_id):
        payload = _job_payload_from_supabase(row)
        job_id = payload.get("job_id")
        if not job_id:
            continue
        items_by_id[job_id] = payload
        try:
            jobs.save(JobState(**payload))
        except Exception:
            pass
    return sorted(items_by_id.values(), key=lambda item: item.get("created_at", ""))


async def _session_log_entries(session_id: str, session_data: dict) -> list[dict]:
    entries = []
    for job in await _session_jobs(session_id):
        for event in job.get("eventos", []):
            entries.append(
                {
                    "ts": event.get("ts") or job.get("updated_at"),
                    "sid": session_id,
                    "level": "info",
                    "agent": event.get("agente", "Kemy"),
                    "message": event.get("msg", ""),
                    "progress": event.get("progresso"),
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                }
            )
        if job.get("erro"):
            entries.append(
                {
                    "ts": job.get("updated_at"),
                    "sid": session_id,
                    "level": "error",
                    "agent": "Kemy",
                    "message": job.get("erro"),
                    "progress": job.get("progresso"),
                    "job_id": job.get("job_id"),
                    "status": job.get("status"),
                }
            )
    for item in session_data.get("historico", []):
        entries.append(
            {
                "ts": item.get("ts") or session_data.get("updated_at"),
                "sid": session_id,
                "level": "info",
                "agent": item.get("role", "message"),
                "message": str(item.get("content") or item.get("resumo") or "")[:500],
                "progress": None,
                "job_id": None,
                "status": "message",
            }
        )
    return sorted(entries, key=lambda item: item.get("ts") or "")


async def _session_analytics(session_id: str, session_data: dict) -> dict:
    jobs_for_session = await _session_jobs(session_id)
    completed = [job for job in jobs_for_session if job.get("status") == "done"]
    failed = [job for job in jobs_for_session if job.get("status") == "error"]
    providers: dict[str, int] = {}
    modes: dict[str, int] = {}
    generated_files = 0
    tool_usage: dict[str, int] = {}

    for job in jobs_for_session:
        modes[job.get("modo", "coding")] = modes.get(job.get("modo", "coding"), 0) + 1
        result = job.get("resultado") or {}
        provider = result.get("provider") or "unknown"
        providers[provider] = providers.get(provider, 0) + 1
        generated_files += len(result.get("files") or [])
        for tool_name in result.get("tools_used") or []:
            tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

    message_count = len(session_data.get("historico", []))
    total = len(jobs_for_session)
    success_rate = round((len(completed) / total) * 100, 1) if total else 0.0
    return {
        "session_id": session_id,
        "total_jobs": total,
        "completed_jobs": len(completed),
        "failed_jobs": len(failed),
        "message_count": message_count,
        "generated_files": generated_files,
        "success_rate_pct": success_rate,
        "by_mode": modes,
        "by_provider": providers,
        "tool_usage": tool_usage,
        "latest_activity": session_data.get("updated_at", ""),
    }


async def _hydrate_session_from_supabase(session_id: str, owner: str | None) -> dict | None:
    if not owner:
        return None
    session = await supabase_auth.get_session(session_id, owner)
    if not session:
        return None
    messages = await supabase_auth.list_messages(session_id)
    historico = []
    compact_context = {}
    last_mode = ""
    last_github_repo = ""
    for item in messages:
        metadata = item.get("metadata") or {}
        files = metadata.get("files", [])
        compact_context = metadata.get("context_snapshot") or compact_context
        if metadata.get("mode"):
            last_mode = str(metadata.get("mode"))
        if metadata.get("github_repo"):
            last_github_repo = str(metadata.get("github_repo"))
        historico.append(
            {
                "ts": item.get("created_at") or utcnow(),
                "role": item.get("role"),
                "content": item.get("content", ""),
                "request_parts": metadata.get("request_parts", []),
                "context_snapshot": metadata.get("context_snapshot", {}),
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "tools_used": metadata.get("tools_used", []),
                "image_url": metadata.get("image_url"),
                "image_data_url": metadata.get("image_data_url"),
                "files": files,
                "preview_url": metadata.get("preview_url"),
                "mode": metadata.get("mode"),
                "site_snapshot": metadata.get("site_snapshot"),
                "workspace_snapshot": metadata.get("workspace_snapshot"),
                "github_repo": metadata.get("github_repo"),
                "result": {
                    "summary": metadata.get("summary"),
                    "image_url": metadata.get("image_url"),
                    "image_data_url": metadata.get("image_data_url"),
                    "provider": metadata.get("provider"),
                    "model": metadata.get("model"),
                    "files": files,
                    "document_title": metadata.get("document_title"),
                    "preview_url": metadata.get("preview_url"),
                    "mode": metadata.get("mode"),
                    "site_snapshot": metadata.get("site_snapshot"),
                    "workspace_snapshot": metadata.get("workspace_snapshot"),
                    "github_repo": metadata.get("github_repo"),
                },
            }
        )
    return _cache_session_payload(
        {
            "session_id": session.get("id"),
            "owner": session.get("owner_email") or owner,
            "title": session.get("title") or "Nova conversa",
            "historico": historico,
            "contexto_compacto": compact_context,
            "last_mode": last_mode,
            "last_github_repo": last_github_repo,
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at"),
        }
    )


async def _load_session_for_owner(session_id: str, owner: str | None) -> dict | None:
    data = storage.get_json(f"session:{session_id}")
    if data:
        if data.get("owner") and owner and data.get("owner") != owner:
            return None
        return data
    return await _hydrate_session_from_supabase(session_id, owner)


def _job_payload_from_supabase(row: dict) -> dict:
    return {
        "job_id": row.get("id"),
        "session_id": row.get("session_id"),
        "status": row.get("status") or "done",
        "etapa": row.get("stage") or "Concluido",
        "progresso": row.get("progress") if row.get("progress") is not None else 100,
        "pedido": row.get("prompt") or "",
        "modo": row.get("mode") or "coding",
        "created_at": row.get("created_at") or utcnow(),
        "updated_at": row.get("updated_at") or row.get("created_at") or utcnow(),
        "resultado": row.get("result"),
        "erro": row.get("error"),
        "eventos": row.get("events") or [],
        "anexos": [],
    }


async def _load_job(job_id: str) -> JobState | None:
    local = jobs.get(job_id)
    if local:
        return local
    row = await jobs.supabase.get_job(job_id)
    if not row:
        return None
    try:
        job = JobState(**_job_payload_from_supabase(row))
    except Exception:
        return None
    jobs.save(job)
    return job


def _safe_artifact_name(path_value: str) -> str:
    return str(path_value or "").replace("\\", "/").strip().lstrip("/").replace("/", "__")


def _is_textual_mime(mime_type: str) -> bool:
    lowered = (mime_type or "").lower()
    return lowered.startswith("text/") or lowered in {
        "application/json",
        "application/javascript",
        "application/xml",
        "image/svg+xml",
    }


def _workspace_language(path_value: str) -> str:
    suffix = Path(path_value or "").suffix.lower().lstrip(".")
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "jsx": "jsx",
        "tsx": "tsx",
        "html": "html",
        "css": "css",
        "json": "json",
        "md": "markdown",
        "yml": "yaml",
        "yaml": "yaml",
        "sql": "sql",
        "sh": "bash",
    }
    return mapping.get(suffix, suffix or "text")


def _extract_text_from_file_row(row: dict, max_chars: int = 140000) -> str:
    mime_type = row.get("mime_type") or ""
    if not _is_textual_mime(mime_type):
        return ""
    content = row.get("content")
    if content is not None:
        return str(content)[:max_chars]
    encoded = row.get("content_base64")
    if not encoded:
        return ""
    try:
        decoded = base64.b64decode(encoded)
    except Exception:
        return ""
    return decoded.decode("utf-8", errors="replace")[:max_chars]


def _workspace_file_payload(
    file_item: dict,
    default_job_id: str = "",
    include_content: bool = True,
    max_chars: int = 140000,
) -> dict | None:
    path_value = str(file_item.get("relative_path") or file_item.get("path") or file_item.get("name") or "").replace("\\", "/").strip().lstrip("/")
    if not path_value:
        return None
    name = str(file_item.get("name") or Path(path_value).name)
    mime_type = str(file_item.get("mime_type") or guess_type(name)[0] or "text/plain")
    download_url = str(file_item.get("download_url") or "")
    job_id = default_job_id
    if download_url.startswith("/api/artefatos/"):
        parts = [part for part in download_url.split("/") if part]
        if len(parts) >= 3:
            job_id = parts[2]
    payload = {
        "path": path_value,
        "name": name,
        "mime_type": mime_type,
        "language": str(file_item.get("language") or _workspace_language(path_value)),
        "download_url": download_url,
        "job_id": job_id,
    }
    size_value = file_item.get("size_bytes")
    if size_value is not None:
        payload["size_bytes"] = size_value
    if include_content and _is_textual_mime(mime_type):
        text = str(file_item.get("content") or "")[:max_chars]
        if text:
            payload["content"] = text
    return payload


def _is_publishable_text_file(path_value: str, mime_type: str = "") -> bool:
    path = str(path_value or "").lower()
    mime = str(mime_type or "").lower()
    if path.endswith((".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".json", ".txt", ".md", ".svg")):
        return True
    if mime.startswith("text/"):
        return True
    return mime in {"application/json", "application/javascript", "image/svg+xml"}


def _safe_publish_path(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip().lstrip("/")
    if not raw:
        return ""
    parts = [segment for segment in raw.split("/") if segment not in {"", "."}]
    if not parts or any(segment == ".." for segment in parts):
        return ""
    normalized = "/".join(parts)
    if normalized.startswith(".git/") or normalized == ".git":
        return ""
    return normalized


def _collect_publish_files(data: dict) -> list[dict]:
    candidate = _latest_workspace_candidate(data) or {}
    merged: dict[str, dict] = {}

    def add_entry(path: str, content: str, mime_type: str = "") -> None:
        safe_path = _safe_publish_path(path)
        if not safe_path:
            return
        if not _is_publishable_text_file(safe_path, mime_type):
            return
        text = str(content or "")
        if not text.strip():
            return
        merged[safe_path.lower()] = {"path": safe_path, "content": text, "mime_type": mime_type or guess_type(safe_path)[0] or "text/plain"}

    for file_item in candidate.get("files") or []:
        if not isinstance(file_item, dict):
            continue
        path = str(file_item.get("relative_path") or file_item.get("path") or file_item.get("name") or "")
        add_entry(path, str(file_item.get("content") or ""), str(file_item.get("mime_type") or ""))

    workspace_snapshot = candidate.get("workspace_snapshot") or {}
    if isinstance(workspace_snapshot, dict):
        for row in workspace_snapshot.get("files") or []:
            if not isinstance(row, dict):
                continue
            add_entry(
                str(row.get("path") or row.get("relative_path") or row.get("name") or ""),
                str(row.get("content") or ""),
                guess_type(str(row.get("path") or row.get("relative_path") or row.get("name") or ""))[0] or "text/plain",
            )

    site_snapshot = candidate.get("site_snapshot") or {}
    if isinstance(site_snapshot, dict):
        for row in site_snapshot.get("files") or []:
            if not isinstance(row, dict):
                continue
            add_entry(
                str(row.get("path") or row.get("relative_path") or row.get("name") or ""),
                str(row.get("content") or ""),
                guess_type(str(row.get("path") or row.get("relative_path") or row.get("name") or ""))[0] or "text/plain",
            )

    files = list(merged.values())
    files.sort(key=lambda item: (0 if str(item.get("path", "")).lower().endswith("preview.html") else 1, str(item.get("path", "")).lower()))
    return files


def _latest_workspace_candidate(session_data: dict) -> dict | None:
    history = session_data.get("historico") or []
    for item in reversed(history):
        if item.get("role") != "assistant":
            continue
        result = item.get("result") or {}
        files = item.get("files") or result.get("files") or []
        site_snapshot = item.get("site_snapshot") or result.get("site_snapshot")
        workspace_snapshot = item.get("workspace_snapshot") or result.get("workspace_snapshot")
        preview_url = result.get("preview_url") or item.get("preview_url")
        mode_hint = str(item.get("mode") or result.get("mode") or session_data.get("last_mode") or "coding").lower()
        has_project_file = _has_project_workspace_signal(files, site_snapshot, workspace_snapshot, preview_url)
        if (files or site_snapshot or workspace_snapshot or preview_url) and has_project_file:
            return {
                "mode": mode_hint,
                "summary": result.get("summary") or item.get("content") or "",
                "artifact_title": result.get("artifact_title") or result.get("document_title") or "",
                "preview_url": preview_url or "",
                "project_archive_url": result.get("project_archive_url") or "",
                "files": files if isinstance(files, list) else [],
                "site_snapshot": site_snapshot if isinstance(site_snapshot, dict) else None,
                "workspace_snapshot": workspace_snapshot if isinstance(workspace_snapshot, dict) else None,
                "updated_at": item.get("ts") or session_data.get("updated_at"),
            }
        # Nao recuar para respostas antigas com preview quando a ultima resposta
        # ja nao trouxe artifact/workspace; evita abrir template fora de contexto.
        return None
    return None


def _has_project_workspace_signal(files: list, site_snapshot: Any, workspace_snapshot: Any, preview_url: Any) -> bool:
    if preview_url or isinstance(site_snapshot, dict):
        return True
    if isinstance(workspace_snapshot, dict) and workspace_snapshot.get("files"):
        return True
    project_suffixes = (".html", ".css", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".py", ".sql", ".yml", ".yaml", ".zip")
    document_only_suffixes = (".docx", ".pdf", ".pptx")
    saw_file = False
    saw_project = False
    saw_document = False
    for item in files or []:
        if not isinstance(item, dict):
            continue
        saw_file = True
        name = str(item.get("path") or item.get("relative_path") or item.get("name") or "").lower()
        if name.endswith(project_suffixes):
            saw_project = True
        if name.endswith(document_only_suffixes):
            saw_document = True
    return saw_project or (saw_file and not saw_document)


def _assistant_has_workspace_signal(item: dict) -> bool:
    result = item.get("result") or {}
    files = item.get("files") or result.get("files") or []
    site_snapshot = item.get("site_snapshot") or result.get("site_snapshot")
    workspace_snapshot = item.get("workspace_snapshot") or result.get("workspace_snapshot")
    preview_url = item.get("preview_url") or result.get("preview_url")
    return _has_project_workspace_signal(files, site_snapshot, workspace_snapshot, preview_url)


def _pick_primary_html(files: list[dict]) -> str:
    if not files:
        return ""
    preferred = ("preview.html", "index.html")
    lowered_paths = {str(item.get("path") or "").lower(): item for item in files}
    for name in preferred:
        for path, item in lowered_paths.items():
            if path.endswith(name):
                return str(item.get("path") or "")
    for item in files:
        path = str(item.get("path") or "").lower()
        if path.endswith(".html"):
            return str(item.get("path") or "")
    return ""


def _publish_internal_site(
    slug: str,
    files: list[dict],
    owner: str | None,
    session_id: str,
) -> dict:
    site_dir = published_sites_root / slug
    if site_dir.exists():
        shutil.rmtree(site_dir, ignore_errors=True)
    site_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for item in files:
        path = _safe_publish_path(str(item.get("path") or ""))
        content = str(item.get("content") or "")
        if not path or not content:
            continue
        target = site_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1

    primary = _pick_primary_html(files)
    if primary and primary.lower() != "index.html":
        source = site_dir / _safe_publish_path(primary)
        if source.exists():
            (site_dir / "index.html").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    if not (site_dir / "index.html").exists():
        fallback_title = slug.replace("-", " ").title()
        (site_dir / "index.html").write_text(
            (
                "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{fallback_title}</title></head><body>"
                f"<h1>{fallback_title}</h1><p>Site publicado pela Kemy AI.</p></body></html>"
            ),
            encoding="utf-8",
        )
        written += 1

    metadata = {
        "slug": slug,
        "owner": owner,
        "session_id": session_id,
        "file_count": written,
        "created_at": utcnow(),
        "primary_html": primary or "index.html",
    }
    (site_dir / "_meta.json").write_text(yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False), encoding="utf-8")

    base = str(settings.app_public_url or "").rstrip("/")
    preview_path = f"{SITE_PUBLIC_PREFIX}/{slug}/"
    live_url = f"{base}{preview_path}" if base else preview_path
    return {
        "slug": slug,
        "target": "internal",
        "preview_url": preview_path,
        "live_url": live_url,
        "subdomain_url": _site_subdomain_url(slug),
        "file_count": written,
        "notes": "Configure wildcard DNS para usar subdominio automatico." if _site_subdomain_url(slug) else "Publicado no host interno da Kemy.",
    }


async def _publish_site_to_vercel(slug: str, files: list[dict]) -> dict:
    if not settings.vercel_token:
        raise HTTPException(400, "VERCEL_TOKEN nao configurado no servidor.")
    payload_files = []
    for item in files:
        path = _safe_publish_path(str(item.get("path") or ""))
        content = str(item.get("content") or "")
        if not path or not content:
            continue
        payload_files.append({"file": path, "data": content})
    if not payload_files:
        raise HTTPException(400, "Nenhum arquivo textual disponivel para deploy no Vercel.")

    params = {}
    if settings.vercel_team_id:
        params["teamId"] = settings.vercel_team_id
    body: dict[str, object] = {
        "name": slug,
        "files": payload_files,
        "target": "production",
        "projectSettings": {"framework": None},
    }
    if settings.vercel_project_id:
        body["project"] = settings.vercel_project_id
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(
            "https://api.vercel.com/v13/deployments",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {settings.vercel_token}", "Content-Type": "application/json"},
        )
    if response.status_code not in {200, 201}:
        try:
            payload = response.json()
            message = payload.get("error", {}).get("message") or payload.get("message") or response.text
        except Exception:
            message = response.text
        raise HTTPException(502, f"Falha no deploy Vercel: {message}")
    payload = response.json()
    deployment_url = payload.get("url")
    if deployment_url and not str(deployment_url).startswith("http"):
        deployment_url = f"https://{deployment_url}"
    preview_url = str(payload.get("inspectorUrl") or deployment_url or "")
    return {
        "slug": slug,
        "target": "vercel",
        "preview_url": preview_url or deployment_url or "",
        "live_url": deployment_url or "",
        "subdomain_url": None,
        "file_count": len(payload_files),
        "notes": "Deploy publicado no Vercel.",
    }


def _match_remote_artifact(rows: list[dict], filename: str) -> dict | None:
    for row in reversed(rows):
        candidates = {
            str(row.get("name") or ""),
            str(row.get("path") or ""),
            _safe_artifact_name(str(row.get("path") or "")),
        }
        if filename in candidates:
            return row
    return None


def _remote_artifact_response(row: dict, filename: str) -> Response:
    media_type = row.get("mime_type") or guess_type(filename)[0] or "application/octet-stream"
    headers = {}
    disposition = "attachment" if media_type == "application/zip" else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    encoded = row.get("content_base64")
    if encoded:
        return Response(content=base64.b64decode(encoded), media_type=media_type, headers=headers)
    content = row.get("content")
    if content is not None:
        return Response(content=str(content), media_type=media_type, headers=headers)
    raise HTTPException(404, "Arquivo remoto indisponivel.")


def _published_file_response(slug: str, asset_path: str = "") -> Response:
    site_dir = published_sites_root / _sanitize_slug(slug)
    if not site_dir.exists():
        raise HTTPException(404, "Site publicado nao encontrado.")
    rel = str(asset_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        rel = "index.html"
    target = (site_dir / rel).resolve()
    try:
        target.relative_to(site_dir.resolve())
    except Exception:
        raise HTTPException(400, "Caminho invalido.")
    if not target.exists() or not target.is_file():
        if rel != "index.html":
            fallback = site_dir / "index.html"
            if fallback.exists():
                target = fallback
            else:
                raise HTTPException(404, "Arquivo do site nao encontrado.")
        else:
            raise HTTPException(404, "Arquivo do site nao encontrado.")
    media_type = guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type)


@app.get("/")
async def root_page(request: Request):
    host = (request.headers.get("host") or "").split(":")[0].strip().lower()
    base_domain = str(settings.site_base_domain or "").strip().lower().strip("/")
    if base_domain and host and host.endswith(f".{base_domain}"):
        slug = host[: -(len(base_domain) + 1)].strip(".")
        if slug:
            return _published_file_response(slug, "index.html")
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"nome": settings.app_name, "versao": settings.version, "docs": "/docs"}


@app.get(f"{SITE_PUBLIC_PREFIX}" + "/{slug}")
@app.get(f"{SITE_PUBLIC_PREFIX}" + "/{slug}/")
async def published_site_root(slug: str):
    return _published_file_response(slug, "index.html")


@app.get(f"{SITE_PUBLIC_PREFIX}" + "/{slug}/{asset_path:path}")
async def published_site_asset(slug: str, asset_path: str):
    return _published_file_response(slug, asset_path)


@app.get("/api/status")
async def status():
    supabase_health = await supabase_auth.healthcheck()
    return {
        "nome": settings.app_name,
        "versao": settings.version,
        "status": "online",
        "free_only": settings.free_only,
        "llm_mode": settings.llm_mode,
        "storage": "supabase+memory-cache" if settings.supabase_enabled else storage.backend,
        "cache": storage.status(),
        "supabase": settings.supabase_enabled,
        "supabase_health": supabase_health,
        "persistent_sessions": bool(supabase_health.get("connected")),
        "persistent_artifacts": bool(supabase_health.get("connected")),
        "providers": settings.configured_providers,
        "tools": settings.configured_tools,
        "fallback_routes": jobs.router.ROUTES,
        "adaptive_routes": {
            "coding": jobs.router.route_debug("coding"),
            "site": jobs.router.route_debug("site"),
            "documento": jobs.router.route_debug("documento"),
        },
        "response_cache": {
            "enabled": settings.response_cache_enabled,
            "ttl_seconds": settings.response_cache_ttl_seconds,
            "items": len(jobs.response_cache),
        },
        "site_publishing": {
            "public_prefix": SITE_PUBLIC_PREFIX,
            "base_domain": settings.site_base_domain or "",
            "vercel_enabled": bool(settings.vercel_token),
        },
        "openrouter_free": jobs.router.openrouter_free_state,
        "gemini": jobs.router.gemini_state,
    }


@app.post("/api/auth/login")
async def login(payload: dict, response: Response, request: Request):
    email = str(payload.get("email") or payload.get("username", "")).strip().lower()
    password = str(payload.get("password", ""))
    stored_user = find_user(storage, email)
    valid_local = email == settings.auth_user and password == settings.auth_password
    valid_registered = bool(stored_user and verify_password(password, stored_user.get("password_hash", "")))
    valid_supabase = False
    if not (valid_local or valid_registered):
        try:
            valid_supabase = await supabase_auth.sign_in_password(email, password)
        except Exception:
            valid_supabase = False
    if not (valid_local or valid_registered or valid_supabase):
        raise HTTPException(401, "Login invalido.")
    token = create_token(email, settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return {"status": "ok", "user": email}


@app.post("/api/auth/register")
async def register(payload: dict, response: Response, request: Request):
    email = str(payload.get("email") or payload.get("username", "")).strip().lower()
    password = str(payload.get("password", ""))
    name = str(payload.get("name", "")).strip()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1] or len(password) < 8:
        raise HTTPException(400, "Email invalido ou senha menor que 8 caracteres.")
    existing = find_user(storage, email)
    if existing:
        if verify_password(password, existing.get("password_hash", "")):
            token = create_token(email, settings)
            response.set_cookie(
                COOKIE_NAME,
                token,
                max_age=MAX_AGE,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="lax",
            )
            return {"status": "ok", "user": email, "message": "Conta existente acessada."}
        raise HTTPException(409, "Email ja cadastrado. Use entrar ou recupere a senha.")
    supabase_created = False
    try:
        created = await supabase_auth.create_auth_user(email, password, name)
        supabase_created = bool(created)
    except Exception:
        supabase_created = False
    create_user(storage, email, password, name)
    token = create_token(email, settings)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return {"status": "ok", "user": email, "persistent_auth": supabase_created}


@app.post("/api/auth/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"status": "ok"}


@app.get("/api/auth/me")
async def me(request: Request):
    subject = token_subject(request.cookies.get(COOKIE_NAME), settings)
    return {"authenticated": bool(subject), "user": subject}


@app.post("/api/sessao/nova")
async def nova_sessao(request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    sid = str(uuid.uuid4())
    now = utcnow()
    _cache_session_payload(
        {
            "session_id": sid,
            "owner": owner,
            "title": "Nova conversa",
            "historico": [],
            "created_at": now,
            "updated_at": now,
        }
    )
    await jobs.supabase.insert_session(sid, owner_email=owner, title="Nova conversa", created_at=now, updated_at=now)
    return {"session_id": sid, "mensagem": "Kemy AI pronta para codar na nuvem."}


@app.get("/api/sessao/{sid}/historico")
async def historico(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    return data


@app.get("/api/sessao/{sid}/contexto")
async def contexto_sessao(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    return {
        "session_id": sid,
        "contexto_compacto": data.get("contexto_compacto") or {},
        "memoria": data.get("memoria", []),
    }


@app.get("/api/sessao/{sid}/workspace")
async def workspace_sessao(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")

    candidate = _latest_workspace_candidate(data) or {}
    files_by_path: dict[str, dict] = {}
    source_job_id = ""

    def add_files(items: list[dict], default_job_id: str = "", include_content: bool = True) -> None:
        for raw_item in items or []:
            if not isinstance(raw_item, dict):
                continue
            payload = _workspace_file_payload(
                raw_item,
                default_job_id=default_job_id,
                include_content=include_content,
            )
            if not payload:
                continue
            path_key = payload["path"].lower()
            existing = files_by_path.get(path_key)
            if not existing:
                files_by_path[path_key] = payload
                continue
            if not existing.get("download_url") and payload.get("download_url"):
                existing["download_url"] = payload["download_url"]
            if not existing.get("job_id") and payload.get("job_id"):
                existing["job_id"] = payload["job_id"]
            if not existing.get("content") and payload.get("content"):
                existing["content"] = payload["content"]

    add_files(candidate.get("files") or [], include_content=True)

    workspace_snapshot = candidate.get("workspace_snapshot") if isinstance(candidate.get("workspace_snapshot"), dict) else {}
    for item in workspace_snapshot.get("files") or []:
        if not isinstance(item, dict):
            continue
        add_files(
            [
                {
                    "relative_path": item.get("path") or item.get("relative_path") or item.get("name"),
                    "name": Path(str(item.get("path") or item.get("relative_path") or item.get("name") or "")).name,
                    "mime_type": guess_type(str(item.get("path") or item.get("relative_path") or item.get("name") or ""))[0] or "text/plain",
                    "language": _workspace_language(str(item.get("path") or item.get("relative_path") or item.get("name") or "")),
                    "content": item.get("content"),
                }
            ],
            include_content=True,
        )

    site_snapshot = candidate.get("site_snapshot") if isinstance(candidate.get("site_snapshot"), dict) else {}
    for item in site_snapshot.get("files") or []:
        if not isinstance(item, dict):
            continue
        add_files(
            [
                {
                    "relative_path": item.get("path"),
                    "name": Path(str(item.get("path") or "")).name,
                    "mime_type": guess_type(str(item.get("path") or ""))[0] or "text/plain",
                    "language": _workspace_language(str(item.get("path") or "")),
                    "content": item.get("content"),
                }
            ],
            include_content=True,
        )

    history = data.get("historico") or []
    latest_assistant = next((item for item in reversed(history) if item.get("role") == "assistant"), None)
    allow_job_backfill = latest_assistant is None or _assistant_has_workspace_signal(latest_assistant)

    if allow_job_backfill:
        jobs_for_session = await _session_jobs(sid)
        for job in reversed(jobs_for_session):
            result = job.get("resultado") or {}
            files = result.get("files") or []
            if not isinstance(files, list) or not files:
                continue
            source_job_id = str(job.get("job_id") or source_job_id or "")
            add_files(files, default_job_id=source_job_id, include_content=True)
            if not candidate.get("preview_url"):
                candidate["preview_url"] = result.get("preview_url") or ""
            if not candidate.get("project_archive_url"):
                candidate["project_archive_url"] = result.get("project_archive_url") or ""
            if not candidate.get("summary"):
                candidate["summary"] = result.get("summary") or ""
            if not candidate.get("artifact_title"):
                candidate["artifact_title"] = result.get("artifact_title") or result.get("document_title") or ""
            if not candidate.get("mode"):
                candidate["mode"] = job.get("modo") or "coding"
            break

    remote_cache: dict[str, list[dict]] = {}

    async def load_remote_rows(job_id: str) -> list[dict]:
        if not job_id:
            return []
        if job_id not in remote_cache:
            remote_cache[job_id] = await jobs.supabase.list_generated_files(job_id)
        return remote_cache[job_id]

    for payload in files_by_path.values():
        if payload.get("content"):
            continue
        if not _is_textual_mime(payload.get("mime_type") or ""):
            continue
        job_id = str(payload.get("job_id") or source_job_id or "")
        if not job_id:
            continue
        rows = await load_remote_rows(job_id)
        row = _match_remote_artifact(rows, str(payload.get("name") or ""))
        if not row:
            row = _match_remote_artifact(rows, _safe_artifact_name(str(payload.get("path") or "")))
        if not row:
            continue
        text = _extract_text_from_file_row(row)
        if text:
            payload["content"] = text
            if not payload.get("download_url"):
                payload["download_url"] = str(row.get("download_url") or "")

    priority_names = [
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

    def rank_file(item: dict) -> tuple[int, str]:
        lowered = str(item.get("path") or "").lower()
        for index, key in enumerate(priority_names):
            if lowered == key:
                return index, lowered
        return len(priority_names) + 1, lowered

    ordered = sorted(files_by_path.values(), key=rank_file)
    content_budget = 420_000
    returned_files: list[dict] = []
    for item in ordered[:40]:
        copied = dict(item)
        content = str(copied.get("content") or "")
        if content:
            allowed = min(140_000, content_budget)
            if allowed <= 0:
                copied["content"] = ""
            else:
                copied["content"] = content[:allowed]
                content_budget -= len(copied["content"])
        returned_files.append(copied)

    return {
        "session_id": sid,
        "ready": bool(returned_files),
        "mode": candidate.get("mode") or data.get("last_mode") or "coding",
        "summary": candidate.get("summary") or "",
        "artifact_title": candidate.get("artifact_title") or "",
        "preview_url": candidate.get("preview_url") or "",
        "project_archive_url": candidate.get("project_archive_url") or "",
        "context_summary": (data.get("contexto_compacto") or {}).get("summary") or "",
        "source_job_id": source_job_id,
        "updated_at": candidate.get("updated_at") or data.get("updated_at") or "",
        "files": returned_files,
        "file_count": len(returned_files),
        "file_count_total": len(files_by_path),
    }


@app.post("/api/site/publicar", response_model=SitePublishResponse)
async def publicar_site(payload: SitePublishRequest, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    sid = (payload.session_id or "").strip()
    if not sid:
        raise HTTPException(400, "session_id e obrigatorio para publicar.")
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada para publicar.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")

    files = _collect_publish_files(data)
    if not files:
        raise HTTPException(400, "Nao encontrei arquivos textuais do projeto nesta sessao para publicar.")

    title_hint = str(data.get("title") or "site-kemy")
    requested_slug = (payload.slug or "").strip()
    base_slug = _sanitize_slug(requested_slug or title_hint, fallback="site-kemy")
    slug = _allocate_site_slug(base_slug)

    if payload.target == "vercel":
        result = await _publish_site_to_vercel(slug, files)
    else:
        result = _publish_internal_site(slug, files, owner, sid)

    return SitePublishResponse(
        status="ok",
        slug=result["slug"],
        target=result["target"],
        preview_url=str(result.get("preview_url") or ""),
        live_url=str(result.get("live_url") or ""),
        subdomain_url=result.get("subdomain_url"),
        file_count=int(result.get("file_count") or 0),
        notes=result.get("notes"),
    )


@app.get("/api/integracoes/status")
async def integracoes_status():
    return {
        "supabase": settings.supabase_enabled,
        "vercel": bool(settings.vercel_token),
        "github": bool(settings.github_client_id),
        "site_base_domain": settings.site_base_domain or "",
        "site_public_prefix": SITE_PUBLIC_PREFIX,
    }


@app.get("/api/sessao/listar")
async def listar_sessoes(request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    sessions_by_id = {}
    for key in storage.keys("session:"):
        data = storage.get_json(key)
        if not data or (data.get("owner") and data.get("owner") != owner):
            continue
        history = data.get("historico", [])
        preview = _preview_from_history(history)
        sessions_by_id[data.get("session_id")] = {
            "session_id": data.get("session_id"),
            "title": data.get("title") or "Nova conversa",
            "preview": preview,
            "updated_at": data.get("updated_at", ""),
            "created_at": data.get("created_at", ""),
        }
    remote_sessions = await supabase_auth.list_sessions(owner or "")
    for session in remote_sessions:
        session_id = session.get("id")
        had_local = session_id in sessions_by_id
        existing = sessions_by_id.get(session_id, {})
        sessions_by_id[session_id] = {
            "session_id": session_id,
            "title": existing.get("title") or session.get("title") or "Nova conversa",
            "preview": existing.get("preview", ""),
            "updated_at": existing.get("updated_at") or session.get("updated_at", ""),
            "created_at": existing.get("created_at") or session.get("created_at", ""),
        }
        if session_id and not had_local:
            remote_messages = await supabase_auth.list_messages(session_id)
            remote_history = []
            compact_context = {}
            last_mode = ""
            last_github_repo = ""
            for item in remote_messages:
                metadata = item.get("metadata") or {}
                files = metadata.get("files", [])
                compact_context = metadata.get("context_snapshot") or compact_context
                if metadata.get("mode"):
                    last_mode = str(metadata.get("mode"))
                if metadata.get("github_repo"):
                    last_github_repo = str(metadata.get("github_repo"))
                remote_history.append(
                    {
                        "ts": item.get("created_at") or utcnow(),
                        "role": item.get("role"),
                        "content": item.get("content", ""),
                        "request_parts": metadata.get("request_parts", []),
                        "context_snapshot": metadata.get("context_snapshot", {}),
                        "provider": metadata.get("provider"),
                        "model": metadata.get("model"),
                        "tools_used": metadata.get("tools_used", []),
                        "image_url": metadata.get("image_url"),
                        "image_data_url": metadata.get("image_data_url"),
                        "files": files,
                        "preview_url": metadata.get("preview_url"),
                        "mode": metadata.get("mode"),
                        "site_snapshot": metadata.get("site_snapshot"),
                        "workspace_snapshot": metadata.get("workspace_snapshot"),
                        "github_repo": metadata.get("github_repo"),
                        "result": {
                            "summary": metadata.get("summary"),
                            "image_url": metadata.get("image_url"),
                            "image_data_url": metadata.get("image_data_url"),
                            "provider": metadata.get("provider"),
                            "model": metadata.get("model"),
                            "files": files,
                            "document_title": metadata.get("document_title"),
                            "preview_url": metadata.get("preview_url"),
                            "mode": metadata.get("mode"),
                            "site_snapshot": metadata.get("site_snapshot"),
                            "workspace_snapshot": metadata.get("workspace_snapshot"),
                            "github_repo": metadata.get("github_repo"),
                        },
                    }
                )
            sessions_by_id[session_id]["preview"] = _preview_from_history(remote_history)
            _cache_session_payload(
                {
                    "session_id": session_id,
                    "owner": session.get("owner_email") or owner,
                    "title": session.get("title") or "Nova conversa",
                    "historico": remote_history,
                    "contexto_compacto": compact_context,
                    "last_mode": last_mode or existing.get("last_mode"),
                    "last_github_repo": last_github_repo or existing.get("last_github_repo"),
                    "created_at": session.get("created_at"),
                    "updated_at": session.get("updated_at"),
                }
            )
    sessions = sorted(sessions_by_id.values(), key=lambda item: item.get("updated_at", ""), reverse=True)
    return {"sessions": sessions[:50]}


@app.post("/api/sessao/{sid}/limpar")
async def limpar_sessao(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    existing = await _load_session_for_owner(sid, owner) or {}
    if existing.get("owner") and existing.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    now = utcnow()
    _cache_session_payload(
        {
            "session_id": sid,
            "owner": owner,
            "title": existing.get("title", "Nova conversa"),
            "historico": [],
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
    )
    await jobs.supabase.insert_session(
        sid,
        owner_email=owner,
        title=existing.get("title", "Nova conversa"),
        created_at=existing.get("created_at", now),
        updated_at=now,
    )
    return {"status": "ok"}


@app.delete("/api/sessao/{sid}")
async def excluir_sessao(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    existing = await _load_session_for_owner(sid, owner)
    if not existing:
        raise HTTPException(404, "Sessao nao encontrada.")
    if existing.get("owner") and existing.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    storage.delete(f"session:{sid}")
    await jobs.supabase.delete_session(sid)
    return {"status": "ok", "session_id": sid}


@app.post("/api/comando", response_model=JobCreateResponse)
async def comando(cmd: ComandoRequest, background_tasks: BackgroundTasks, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    sid = cmd.session_id or str(uuid.uuid4())
    data = await _load_session_for_owner(sid, owner)
    if not data:
        now = utcnow()
        title = cmd.mensagem.strip()[:58] or "Nova conversa"
        _cache_session_payload(
            {
                "session_id": sid,
                "owner": owner,
                "title": title,
                "historico": [],
                "created_at": now,
                "updated_at": now,
            }
        )
        await jobs.supabase.insert_session(sid, owner_email=owner, title=title, created_at=now, updated_at=now)
    else:
        if data.get("owner") and data.get("owner") != owner:
            raise HTTPException(403, "Sessao de outro usuario.")
    selected_repo = (cmd.github_repo or (data or {}).get("last_github_repo") or "").strip() or None
    session_snapshot = jobs.register_user_turn(sid, cmd.mensagem, owner=owner, github_repo=selected_repo)
    last_user = (session_snapshot.get("historico") or [])[-1] if session_snapshot.get("historico") else {}
    await jobs.supabase.insert_session(
        sid,
        owner_email=owner,
        title=session_snapshot.get("title") or "Nova conversa",
        created_at=session_snapshot.get("created_at"),
        updated_at=session_snapshot.get("updated_at"),
    )
    await jobs.supabase.insert_message(
        sid,
        "user",
        cmd.mensagem,
        {
            "request_parts": last_user.get("request_parts", []),
            "context_snapshot": last_user.get("context_snapshot", {}),
            "modo": cmd.modo,
            "github_repo": selected_repo,
            "queued_at": utcnow(),
        },
    )
    job = jobs.create(
        sid,
        cmd.mensagem,
        cmd.modo,
        attachments=[item.model_dump() for item in cmd.anexos],
        github_repo=selected_repo,
    )
    background_tasks.add_task(jobs.run, job.job_id)
    return {
        "status": "queued",
        "job_id": job.job_id,
        "session_id": sid,
        "status_url": f"/api/jobs/{job.job_id}",
    }


@app.get("/api/jobs")
async def listar_jobs():
    return {"jobs": jobs.list_recent()}


@app.get("/api/cache/status")
async def cache_status():
    return {"cache": storage.status()}


@app.get("/api/logs/{sid}")
async def session_logs(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    logs = await _session_log_entries(sid, data)
    return {"session_id": sid, "total": len(logs), "logs": logs[-250:]}


@app.get("/api/analytics/{sid}")
async def session_analytics(sid: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    data = await _load_session_for_owner(sid, owner)
    if not data:
        raise HTTPException(404, "Sessao nao encontrada.")
    if data.get("owner") and data.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    return await _session_analytics(sid, data)


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = await _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job nao encontrado.")
    return job.model_dump()


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    job = await _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job nao encontrado.")
    session = await _load_session_for_owner(job.session_id, owner)
    if not session:
        raise HTTPException(404, "Sessao nao encontrada.")
    if session.get("owner") and session.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    updated = jobs.cancel(job_id, requested_by=owner or "user")
    if not updated:
        raise HTTPException(404, "Job nao encontrado.")
    await jobs.supabase.insert_job(updated.model_dump())
    return {
        "status": "ok",
        "job_id": job_id,
        "job_status": updated.status,
        "message": "Cancelamento solicitado.",
    }


@app.get("/api/artefatos/{job_id}/{filename}")
async def baixar_artefato(job_id: str, filename: str, request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    job = await _load_job(job_id)
    if not job:
        raise HTTPException(404, "Job nao encontrado.")
    session = await _load_session_for_owner(job.session_id, owner)
    if not session:
        raise HTTPException(404, "Sessao nao encontrada.")
    if session.get("owner") and session.get("owner") != owner:
        raise HTTPException(403, "Sessao de outro usuario.")
    files = (job.resultado or {}).get("files", [])
    match = next((item for item in files if item.get("name") == filename), None)
    if not match:
        remote_match = _match_remote_artifact(await jobs.supabase.list_generated_files(job_id), filename)
        if remote_match:
            return _remote_artifact_response(remote_match, filename)
        raise HTTPException(404, "Arquivo nao encontrado.")
    path = Path(str(match.get("path") or "")).resolve()
    if not path.exists() or path.name != filename:
        remote_match = _match_remote_artifact(await jobs.supabase.list_generated_files(job_id), filename)
        if remote_match:
            return _remote_artifact_response(remote_match, filename)
        raise HTTPException(404, "Arquivo indisponivel.")
    media_type = match.get("mime_type") or guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/api/agente/listar")
async def listar_agentes():
    custom = []
    folder = Path("agentes_customizados")
    folder.mkdir(exist_ok=True)
    for arq in folder.glob("*.yaml"):
        if arq.name.startswith("EXEMPLO"):
            continue
        try:
            custom.append(yaml.safe_load(arq.read_text(encoding="utf-8")))
        except Exception as exc:
            custom.append({"arquivo": arq.name, "erro": str(exc)})
    return {"agentes_fixos": DEFAULT_AGENTS, "agentes_customizados": custom}


@app.post("/api/agente/criar")
async def criar_agente(novo: NovoAgente):
    if novo.motor not in ["gemini", "groq", "cerebras", "openrouter"]:
        raise HTTPException(400, "Motor invalido.")
    slug = re.sub(r"[^a-z0-9_]+", "_", novo.nome.lower()).strip("_")
    if not slug:
        raise HTTPException(400, "Nome invalido.")
    folder = Path("agentes_customizados")
    folder.mkdir(exist_ok=True)
    path = folder / f"{slug}.yaml"
    if path.exists():
        raise HTTPException(409, "Agente ja existe.")
    cfg = novo.model_dump()
    cfg.update({"slug": slug, "criado_em": utcnow()})
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return {"status": "criado", "agente": cfg}


@app.delete("/api/agente/{slug}")
async def remover_agente(slug: str):
    path = Path("agentes_customizados") / f"{slug}.yaml"
    if not path.exists():
        raise HTTPException(404, "Agente nao encontrado.")
    path.unlink()
    return {"status": "removido", "slug": slug}

@app.get("/api/github/connect")
async def github_connect(request: Request):
    if not settings.github_client_id:
        raise HTTPException(status_code=400, detail="GitHub Client ID não configurado.")
    state = str(uuid.uuid4())
    base = str(request.base_url).rstrip("/")
    redirect_uri = f"{base}/api/github/callback"
    url = f"https://github.com/login/oauth/authorize?client_id={settings.github_client_id}&redirect_uri={redirect_uri}&state={state}&scope=repo,user:email"
    return RedirectResponse(url)

@app.get("/api/github/callback")
async def github_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if not code:
        error_detail = error or "Parâmetro 'code' ausente. Acesse via botão 'Conectar GitHub' no app."
        html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>GitHub OAuth – Kemy AI</title>
        <style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0d1117;color:#e6edf3}}
        .card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:480px;text-align:center}}
        h1{{color:#ff7b72;margin:0 0 12px}}p{{color:#8b949e;margin:8px 0}}</style></head>
        <body><div class="card"><h1>⚠️ OAuth Incompleto</h1><p>{error_detail}</p>
        <p>Para conectar o GitHub, use o botão na barra de navegação do Kemy AI.</p>
        <script>setTimeout(()=>{{if(window.opener)window.close();else window.location.href='/'}},4000)</script>
        </div></body></html>"""
        return Response(html, media_type="text/html")

    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None

    base = str(request.base_url).rstrip("/")
    payload = {
        "client_id": settings.github_client_id,
        "client_secret": settings.github_client_secret,
        "code": code,
        "redirect_uri": f"{base}/api/github/callback",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(
                "https://github.com/login/oauth/access_token",
                json=payload,
                headers={"Accept": "application/json"},
            )
            data = res.json()
    except Exception as exc:
        err_html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>Erro – Kemy AI</title>
        <style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0d1117;color:#e6edf3}}
        .card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:480px;text-align:center}}
        h1{{color:#ff7b72;margin:0 0 12px}}p{{color:#8b949e;margin:8px 0}}</style></head>
        <body><div class="card"><h1>❌ Falha de Rede</h1>
        <p>Não foi possível contatar a API do GitHub.</p>
        <p style="font-size:12px;color:#6e7681">{str(exc)[:200]}</p>
        </div></body></html>"""
        return Response(err_html, media_type="text/html", status_code=502)

    token = data.get("access_token")
    if not token:
        gh_error = data.get("error", "unknown")
        gh_desc = data.get("error_description", "Sem descrição")
        err_html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>Erro GitHub – Kemy AI</title>
        <style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0d1117;color:#e6edf3}}
        .card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;max-width:520px;text-align:center}}
        h1{{color:#ff7b72;margin:0 0 12px}}p{{color:#8b949e;margin:8px 0}}code{{background:#21262d;padding:4px 8px;border-radius:6px;color:#f0883e}}</style></head>
        <body><div class="card"><h1>❌ Token não obtido</h1>
        <p>Erro do GitHub: <code>{gh_error}</code></p><p>{gh_desc}</p>
        <p style="font-size:12px;color:#6e7681">Verifique se GITHUB_CLIENT_ID e GITHUB_CLIENT_SECRET estão corretos no Render.</p>
        <script>setTimeout(()=>{{if(window.opener)window.close();}},6000)</script>
        </div></body></html>"""
        return Response(err_html, media_type="text/html", status_code=400)

    if user:
        storage.set_json(f"github_config:{user}", {"token": token}, ttl=30 * 86400)

    html = f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><title>GitHub Conectado – Kemy AI</title>
    <style>body{{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;background:#0d1117;color:#e6edf3}}
    .card{{background:#161b22;border:1px solid #238636;border-radius:12px;padding:32px;max-width:480px;text-align:center}}
    h1{{color:#3fb950;margin:0 0 12px}}p{{color:#8b949e;margin:8px 0}}</style></head>
    <body><div class="card"><h1>✅ GitHub Conectado!</h1>
    <p>Conta vinculada com sucesso. Esta janela vai fechar automaticamente.</p></div>
    <script>
        if (window.opener) {{
            window.opener.postMessage('github_connected', '*');
            window.close();
        }} else {{
            window.location.href = '/?github=connected&user={user or ""}';
        }}
    </script></body></html>"""
    return Response(html, media_type="text/html")



@app.get("/api/github/me")
async def github_me(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None
    oauth_available = bool(settings.github_client_id)
    if not user:
        return JSONResponse({"connected": False, "oauth_available": oauth_available})
    config = storage.get_json(f"github_config:{user}") or {}
    token = config.get("token")
    if not token:
        return JSONResponse({"connected": False, "oauth_available": oauth_available})
    async with httpx.AsyncClient() as client:
        res = await client.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if res.status_code != 200:
            return JSONResponse({"connected": False, "oauth_available": oauth_available})
        data = res.json()
        return JSONResponse({
            "connected": True,
            "oauth_available": oauth_available,
            "github_login": data.get("login"),
            "github_avatar": data.get("avatar_url")
        })

@app.post("/api/github/disconnect")
async def github_disconnect(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None
    if user:
        storage.delete(f"github_config:{user}")
    return JSONResponse({"status": "disconnected"})


@app.get("/api/github/repos")
async def github_repos(request: Request):
    cookie = request.cookies.get(COOKIE_NAME)
    user = verify_token(cookie, settings) if cookie else None
    if not user:
        return JSONResponse({"repos": []})
    
    config = storage.get_json(f"github_config:{user}") or {}
    token = config.get("token")
    if not token:
        return JSONResponse({"repos": []})
    
    async with httpx.AsyncClient() as client:
        res = await client.get("https://api.github.com/user/repos?sort=updated&per_page=100", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        if res.status_code != 200:
            return JSONResponse({"repos": []})
        data = res.json()
        repos = [{"name": r["full_name"], "url": r["clone_url"], "default_branch": r["default_branch"]} for r in data]
        return JSONResponse({"repos": repos})
