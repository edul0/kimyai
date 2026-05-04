from __future__ import annotations

from typing import Any

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

    async def insert_session(self, session_id: str) -> None:
        if not self.enabled:
            return
        try:
            await self._upsert("sessions", {"id": session_id, "title": "Nova sessao"}, "id")
        except Exception:
            return

    async def insert_message(self, session_id: str, role: str, content: str, metadata: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        try:
            await self._insert("messages", {"session_id": session_id, "role": role, "content": content, "metadata": metadata or {}})
        except Exception:
            return

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
