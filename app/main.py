from __future__ import annotations

import re
import uuid
from pathlib import Path

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agents import DEFAULT_AGENTS
from .auth import COOKIE_NAME, MAX_AGE, create_token, create_user, find_user, token_subject, verify_password, verify_token
from .config import get_settings
from .jobs import JobManager, utcnow
from .schemas import ComandoRequest, JobCreateResponse, NovoAgente
from .storage import Storage
from .supabase_store import SupabaseStore

settings = get_settings()
storage = Storage(settings.redis_url)
jobs = JobManager(storage, settings)
supabase_auth = SupabaseStore(settings)

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
    public_paths = ("/static/", "/api/auth/", "/favicon.ico")
    path = request.url.path
    if path.startswith(public_paths) or path in {"/"}:
        return await call_next(request)
    if path.startswith("/api/") and not verify_token(request.cookies.get(COOKIE_NAME), settings):
        return JSONResponse({"detail": "Nao autenticado."}, status_code=401)
    return await call_next(request)

static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _cache_session_payload(payload: dict) -> dict:
    session_id = payload.get("session_id") or payload.get("id")
    data = {
        "session_id": session_id,
        "owner": payload.get("owner") or payload.get("owner_email"),
        "title": payload.get("title") or "Nova conversa",
        "historico": payload.get("historico", []),
        "memoria": payload.get("memoria", []),
        "created_at": payload.get("created_at") or utcnow(),
        "updated_at": payload.get("updated_at") or payload.get("created_at") or utcnow(),
    }
    storage.set_json(f"session:{session_id}", data, ttl=settings.session_ttl_seconds)
    return data


async def _hydrate_session_from_supabase(session_id: str, owner: str | None) -> dict | None:
    if not owner:
        return None
    session = await supabase_auth.get_session(session_id, owner)
    if not session:
        return None
    messages = await supabase_auth.list_messages(session_id)
    historico = []
    for item in messages:
        metadata = item.get("metadata") or {}
        historico.append(
            {
                "ts": item.get("created_at") or utcnow(),
                "role": item.get("role"),
                "content": item.get("content", ""),
                "provider": metadata.get("provider"),
                "model": metadata.get("model"),
                "tools_used": metadata.get("tools_used", []),
                "image_url": metadata.get("image_url"),
                "result": {
                    "summary": metadata.get("summary"),
                    "image_url": metadata.get("image_url"),
                    "provider": metadata.get("provider"),
                    "model": metadata.get("model"),
                },
            }
        )
    return _cache_session_payload(
        {
            "session_id": session.get("id"),
            "owner": session.get("owner_email") or owner,
            "title": session.get("title") or "Nova conversa",
            "historico": historico,
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


@app.get("/")
async def root_page():
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"nome": settings.app_name, "versao": settings.version, "docs": "/docs"}


@app.get("/api/status")
async def status():
    return {
        "nome": settings.app_name,
        "versao": settings.version,
        "status": "online",
        "free_only": settings.free_only,
        "llm_mode": settings.llm_mode,
        "storage": storage.backend,
        "supabase": settings.supabase_enabled,
        "providers": settings.configured_providers,
        "tools": settings.configured_tools,
        "fallback_routes": jobs.router.ROUTES,
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


@app.get("/api/sessao/listar")
async def listar_sessoes(request: Request):
    owner = token_subject(request.cookies.get(COOKIE_NAME), settings)
    sessions_by_id = {}
    for key in storage.keys("session:"):
        data = storage.get_json(key)
        if not data or (data.get("owner") and data.get("owner") != owner):
            continue
        history = data.get("historico", [])
        preview = ""
        for item in reversed(history):
            preview = item.get("content") or item.get("usuario") or item.get("resumo") or ""
            if preview:
                break
        sessions_by_id[data.get("session_id")] = {
            "session_id": data.get("session_id"),
            "title": data.get("title") or "Nova conversa",
            "preview": preview[:90],
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
            _cache_session_payload(
                {
                    "session_id": session_id,
                    "owner": session.get("owner_email") or owner,
                    "title": session.get("title") or "Nova conversa",
                    "historico": [],
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
    job = jobs.create(
        sid,
        cmd.mensagem,
        cmd.modo,
        attachments=[item.model_dump() for item in cmd.anexos],
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


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job nao encontrado.")
    return job.model_dump()


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
