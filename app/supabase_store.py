from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings


class SupabaseStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.url = (settings.supabase_url or "").rstrip("/")
        self.key = settings.supabase_service_role_key

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

    async def _insert(self, table: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.url}/rest/v1/kemy.{table}", headers=self.headers(), json=payload)
            response.raise_for_status()

    async def _upsert(self, table: str, payload: dict[str, Any], conflict: str) -> None:
        headers = self.headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.url}/rest/v1/kemy.{table}?on_conflict={conflict}",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

    async def _delete(self, table: str, query: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f"{self.url}/rest/v1/kemy.{table}?{query}", headers=self.headers())
            response.raise_for_status()

    async def _select_many(self, table: str, query: str) -> list[dict[str, Any]]:
        headers = self.headers()
        headers["Accept"] = "application/json"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.url}/rest/v1/kemy.{table}?{query}", headers=headers)
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []

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
