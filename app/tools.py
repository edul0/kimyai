from __future__ import annotations

from typing import Any

from .config import Settings


class ExternalTools:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def enrich(self, prompt: str, mode: str) -> dict[str, Any]:
        context: list[str] = []
        used: list[str] = []

        if mode in {"coding", "site", "planejamento", "documento"}:
            web_context = await self._web_search(prompt)
            if web_context:
                used.extend(web_context["used"])
                context.extend(web_context["snippets"])

        if self.settings.browserless_url or self.settings.browserless_api_key:
            used.append("browserless-ready")
        if self.settings.e2b_api_key:
            used.append("e2b-ready")

        return {"used": used, "context": "\n".join(context)}

    async def _web_search(self, prompt: str) -> dict[str, Any] | None:
        if self.settings.tavily_api_key:
            result = await self._tavily(prompt)
            if result:
                return result
        if self.settings.serper_api_key:
            result = await self._serper(prompt)
            if result:
                return result
        return None

    async def _tavily(self, prompt: str) -> dict[str, Any] | None:
        import httpx

        payload = {
            "api_key": self.settings.tavily_api_key,
            "query": prompt[:400],
            "search_depth": "basic",
            "max_results": 3,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post("https://api.tavily.com/search", json=payload)
                response.raise_for_status()
                data = response.json()
            snippets = [
                f"Tavily: {item.get('title', 'resultado')} - {item.get('content', '')[:500]}"
                for item in data.get("results", [])
            ]
            return {"used": ["tavily"], "snippets": snippets}
        except Exception:
            return None

    async def _serper(self, prompt: str) -> dict[str, Any] | None:
        import httpx

        headers = {"X-API-KEY": self.settings.serper_api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post("https://google.serper.dev/search", headers=headers, json={"q": prompt[:400]})
                response.raise_for_status()
                data = response.json()
            snippets = [
                f"Serper: {item.get('title', 'resultado')} - {item.get('snippet', '')[:500]}"
                for item in data.get("organic", [])[:3]
            ]
            return {"used": ["serper"], "snippets": snippets}
        except Exception:
            return None
