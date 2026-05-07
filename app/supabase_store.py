from __future__ import annotations

import base64
from typing import Any
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings


class SupabaseStore:
    SCHEMA = "kemy"
    MAX_TEXT_CONTENT = 500_000
    MAX_BINARY_BYTES = 4_000_000

    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = (settings.supabase_url or "").rstrip("/")
        self.key = settings.supabase_service_role_key
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def headers(self) -> dict[str, str]:
        return {
            "apikey": self.key or "",
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def _profile_headers(self, method: str) -> dict[str, str]:
        headers = self.headers()
        if method.upper() in {"GET", "HEAD"}:
            headers["Accept-Profile"] = self.SCHEMA
        else:
            headers["Content-Profile"] = self.SCHEMA
        return headers

    async def healthcheck(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "connected": False, "message": "Supabase nao configurado."}
        try:
            rows = await self._select_many("sessions", "select=id&limit=1", timeout=6)
            return {
                "enabled": True,
                "connected": True,
                "schema": self.SCHEMA,
                "message": "Supabase Data API acessivel.",
                "sample_rows": len(rows),
            }
        except Exception as exc:
            self.last_error = self._safe_error(exc)
            return {
                "enabled": True,
                "connected": False,
                "schema": self.SCHEMA,
                "message": self.last_error,
            }

    async def insert_job(self, job: dict[str, Any]) -> None:
        if not self.enabled:
            return
        payload = {
            "id": job["job_id"],
            "session_id": job["session_id"],
            "status": job["status"],
            "mode": job.get("modo", "coding"),
            "prompt": job["pedido"],
            "progress": job["progresso"],
            "stage": job["etapa"],
            "result": job.get("resultado"),
            "error": job.get("erro"),
            "events": job.get("eventos", []),
            "created_at": job.get("created_at"),
            "updated_at": job.get("updated_at"),
        }
        try:
            await self._upsert("jobs", payload, "id")
        except Exception:
            return

    async def insert_session(
        self,
        session_id: str,
        owner_email: str | None = None,
        title: str = "Nova sessao",
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        payload = {"id": session_id, "title": title}
        if owner_email:
            payload["owner_email"] = owner_email
        if created_at:
            payload["created_at"] = created_at
        if updated_at:
            payload["updated_at"] = updated_at
        try:
            await self._upsert("sessions", payload, "id")
        except Exception:
            return

    async def insert_message(self, session_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        try:
            await self._insert("messages", {"session_id": session_id, "role": role, "content": content, "metadata": metadata or {}})
        except Exception:
            return

    async def insert_generated_file(self, job_id: str, file_item: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path_value = file_item.get("relative_path") or file_item.get("name") or ""
        if not path_value:
            return
        name = file_item.get("name") or Path(str(path_value)).name
        mime_type = file_item.get("mime_type") or guess_type(str(name))[0] or "application/octet-stream"
        content = str(file_item.get("content") or "")
        payload = {
            "job_id": job_id,
            "path": path_value,
            "name": name,
            "mime_type": mime_type,
            "download_url": file_item.get("download_url"),
            "language": file_item.get("language"),
            "content": content[: self.MAX_TEXT_CONTENT],
        }
        local_path = Path(str(file_item.get("path") or ""))
        if local_path.exists() and local_path.is_file():
            data = local_path.read_bytes()
            payload["size_bytes"] = len(data)
            if self._is_text_mime(mime_type) and not payload["content"]:
                payload["content"] = data.decode("utf-8", errors="replace")[: self.MAX_TEXT_CONTENT]
            if len(data) <= self.MAX_BINARY_BYTES:
                payload["content_base64"] = base64.b64encode(data).decode("ascii")
        try:
            await self._insert("generated_files", payload)
        except Exception:
            return

    async def delete_session(self, session_id: str) -> None:
        if not self.enabled:
            return
        try:
            await self._delete("sessions", f"id=eq.{session_id}")
        except Exception:
            return

    async def list_sessions(self, owner_email: str) -> list[dict[str, Any]]:
        if not (self.enabled and owner_email):
            return []
        query = (
            "select=id,title,owner_email,created_at,updated_at"
            f"&owner_email=eq.{quote(owner_email, safe='')}"
            "&order=updated_at.desc"
            "&limit=50"
        )
        try:
            return await self._select_many("sessions", query)
        except Exception:
            return []

    async def get_session(self, session_id: str, owner_email: str) -> dict[str, Any] | None:
        if not (self.enabled and session_id and owner_email):
            return None
        query = (
            "select=id,title,owner_email,created_at,updated_at"
            f"&id=eq.{quote(session_id, safe='')}"
            f"&owner_email=eq.{quote(owner_email, safe='')}"
            "&limit=1"
        )
        try:
            rows = await self._select_many("sessions", query)
            return rows[0] if rows else None
        except Exception:
            return None

    async def list_messages(self, session_id: str) -> list[dict[str, Any]]:
        if not (self.enabled and session_id):
            return []
        query = (
            "select=role,content,metadata,created_at"
            f"&session_id=eq.{quote(session_id, safe='')}"
            "&order=created_at.asc"
            "&limit=500"
        )
        try:
            return await self._select_many("messages", query)
        except Exception:
            return []

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        if not (self.enabled and job_id):
            return None
        query = (
            "select=id,session_id,status,mode,prompt,progress,stage,result,error,events,created_at,updated_at"
            f"&id=eq.{quote(job_id, safe='')}"
            "&limit=1"
        )
        try:
            rows = await self._select_many("jobs", query)
            return rows[0] if rows else None
        except Exception:
            return None

    async def list_generated_files(self, job_id: str) -> list[dict[str, Any]]:
        if not (self.enabled and job_id):
            return []
        query = (
            "select=name,path,language,mime_type,download_url,content,content_base64,size_bytes,created_at"
            f"&job_id=eq.{quote(job_id, safe='')}"
            "&order=created_at.asc"
            "&limit=200"
        )
        try:
            return await self._select_many("generated_files", query)
        except Exception:
            return []

    async def _insert(self, table: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.url}/rest/v1/{table}", headers=self._profile_headers("POST"), json=payload)
            response.raise_for_status()

    async def _upsert(self, table: str, payload: dict[str, Any], conflict: str) -> None:
        headers = self._profile_headers("POST")
        headers["Prefer"] = "resolution=merge-duplicates"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.url}/rest/v1/{table}?on_conflict={conflict}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

    async def _delete(self, table: str, query: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(
                f"{self.url}/rest/v1/{table}?{query}",
                headers=self._profile_headers("DELETE"),
            )
            response.raise_for_status()

    async def _select_many(self, table: str, query: str, timeout: int = 20) -> list[dict[str, Any]]:
        headers = self._profile_headers("GET")
        headers["Accept"] = "application/json"
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{self.url}/rest/v1/{table}?{query}", headers=headers)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []

    def _is_text_mime(self, mime_type: str) -> bool:
        lowered = (mime_type or "").lower()
        return lowered.startswith("text/") or lowered in {
            "application/json",
            "application/javascript",
            "application/xml",
            "image/svg+xml",
        }

    def _safe_error(self, exc: Exception) -> str:
        if isinstance(exc, httpx.HTTPStatusError):
            text = exc.response.text[:240].replace(self.key or "", "[secret]")
            return f"HTTP {exc.response.status_code}: {text}"
        return str(exc).replace(self.key or "", "[secret]")[:240]

    async def create_auth_user(self, email: str, password: str, name: str = "") -> dict[str, Any] | None:
        if not self.enabled:
            return None
        payload = {
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"name": name or email},
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.url}/auth/v1/admin/users", headers=self.headers(), json=payload)
            if response.status_code in {400, 409, 422} and "already" in response.text.lower():
                return {"email": email, "exists": True}
            response.raise_for_status()
            return response.json()

    async def sign_in_password(self, email: str, password: str) -> bool:
        if not (self.url and self.settings.supabase_anon_key):
            return False
        headers = {
            "apikey": self.settings.supabase_anon_key,
            "Content-Type": "application/json",
        }
        payload = {"email": email, "password": password}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.url}/auth/v1/token?grant_type=password", headers=headers, json=payload)
            return response.is_success
