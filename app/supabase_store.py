from __future__ import annotations
from typing import Any
from urllib.parse import quote
import httpx
import logging # <- ADICIONADO PARA LOGS REAIS
from .config import Settings

logger = logging.getLogger("kemy.supabase")

# ... (mantenha o __init__, headers e métodos de negócio, vamos alterar apenas a base das requisições) ...

    # Substitua os métodos de requisição base no final do arquivo por estes:

    async def _insert(self, table: str, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.post(f"{self.url}/rest/v1/kemy.{table}", headers=self.headers(), json=payload)
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Erro no Supabase (_insert na tabela {table}): {e.response.text}")
                raise
            except Exception as e:
                logger.error(f"Falha de conexao ao Supabase (_insert): {e}")

    async def _upsert(self, table: str, payload: dict[str, Any], conflict: str) -> None:
        headers = self.headers()
        headers["Prefer"] = "resolution=merge-duplicates"
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.post(
                    f"{self.url}/rest/v1/kemy.{table}?on_conflict={conflict}",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.error(f"Erro no Supabase (_upsert na tabela {table}): {e.response.text}")
            except Exception as e:
                logger.error(f"Falha de conexao ao Supabase (_upsert): {e}")

    async def _delete(self, table: str, query: str) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.delete(f"{self.url}/rest/v1/kemy.{table}?{query}", headers=self.headers())
                response.raise_for_status()
            except Exception as e:
                logger.error(f"Erro ao deletar no Supabase ({table}): {e}")

    async def _select_many(self, table: str, query: str) -> list[dict[str, Any]]:
        headers = self.headers()
        headers["Accept"] = "application/json"
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.get(f"{self.url}/rest/v1/kemy.{table}?{query}", headers=headers)
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, list) else []
            except httpx.HTTPStatusError as e:
                logger.error(f"Erro de query no Supabase ({table}): {e.response.text}")
                return []
            except Exception as e:
                logger.error(f"Erro de conexao no Supabase (_select_many): {e}")
                return []
