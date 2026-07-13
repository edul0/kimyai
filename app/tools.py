from __future__ import annotations

import ast
import operator
import re
from typing import Any

from .config import Settings

# Calculadora determinística: resolve a aritmética do pedido SEM gastar cota de LLM
# (e sem chute de conta — o erro clássico dos modelos). Só operações seguras via AST.
_CALC_TRIGGER = re.compile(
    r"(?i)\b(quanto (é|e|d[áa]|fica)|calcul\w+|some|soma|porcent\w+|percentual|juros|"
    r"desconto de|m[ée]dia (de|entre)|raiz|pot[êe]ncia)\b")
_ALLOWED_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.UAdd: operator.pos, ast.Mod: operator.mod,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
        return _ALLOWED_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("expressao nao permitida")


def maybe_compute(prompt: str) -> str:
    """'quanto é 15% de 2350' / 'calcula 17*23 + 10' -> resultado EXATO, sem LLM."""
    if not _CALC_TRIGGER.search(prompt or "") or not re.search(r"\d", prompt or ""):
        return ""
    t = (prompt or "")[:400]
    # normaliza pt-BR: '15% de 2350' -> '((15/100)*2350)'; vírgula decimal; 'x' como vezes
    t = re.sub(r"(\d+(?:[.,]\d+)?)\s*%\s*(?:de|do|da)\s*(\d+(?:[.,]\d+)?)", r"((\1/100)*\2)", t)
    t = re.sub(r"(\d),(\d)", r"\1.\2", t)
    t = re.sub(r"(?i)(\d)\s*x\s*(\d)", r"\1*\2", t)
    resultados = []
    for e in re.findall(r"[\d.()+\-*/%\s^]{3,}", t):
        e = e.strip().replace("^", "**")
        if not re.search(r"\d\s*[+\-*/%]\s*\d|\)\s*[*+/-]|\*\*", e):
            continue
        try:
            v = _safe_eval(ast.parse(e, mode="eval"))
        except Exception:
            continue
        if abs(v) < 1e15:
            resultados.append((e, v))
    if not resultados:
        return ""
    def fmt(v: float) -> str:
        s = f"{v:,.4f}".rstrip("0").rstrip(".")
        return s.replace(",", "_").replace(".", ",").replace("_", ".")
    linhas = [f"{e} = {fmt(v)}" for e, v in resultados[:3]]
    return ("[RESULTADO CALCULADO — valores EXATOS computados agora; use-os na resposta, "
            "NAO recalcule de cabeca]\n" + "\n".join(linhas))


class ExternalTools:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def enrich(self, prompt: str, mode: str) -> dict[str, Any]:
        import datetime
        context: list[str] = []
        used: list[str] = []

        # Auto datetime context
        now = datetime.datetime.now()
        context.append(f"[SISTEMA] Data e hora atual do servidor: {now.strftime('%Y-%m-%d %H:%M:%S')}")
        used.append("clock")

        calc = maybe_compute(prompt)
        if calc:
            used.append("calculadora")
            context.append(calc)

        if mode in {"coding", "site", "planejamento", "documento", "pesquisa"}:
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
        # GRÁTIS e sem chave: DuckDuckGo HTML + leitura da 1ª página (mesma técnica do desktop).
        return await self._duckduckgo(prompt)

    async def _duckduckgo(self, prompt: str) -> dict[str, Any] | None:
        """Busca gratuita (sem API key) — a Kemy web ganha contexto atual mesmo em FREE_ONLY."""
        import urllib.parse

        import httpx

        try:
            q = urllib.parse.quote((prompt or "")[:200])
            headers = {"User-Agent": "Mozilla/5.0 KemyWeb"}
            async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=True) as client:
                resp = await client.get(f"https://html.duckduckgo.com/html/?q={q}")
                resp.raise_for_status()
                out: list[str] = []
                links: list[str] = []
                for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                                     resp.text, re.DOTALL):
                    title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                    link = m.group(1)
                    dec = re.search(r"uddg=([^&]+)", link)
                    if dec:
                        link = urllib.parse.unquote(dec.group(1))
                    if title:
                        out.append(f"DuckDuckGo: {title} ({link})")
                        links.append(link)
                    if len(out) >= 4:
                        break
                if not out:
                    return None
                # lê a 1ª página (texto real, não só o título) — profundidade de verdade
                if links:
                    try:
                        page = await client.get(links[0])
                        raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
                                     " ", page.text)
                        body = re.search(r"(?is)<(article|main)[^>]*>(.*)</\1>", raw)
                        if body:
                            raw = body.group(2)
                        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()
                        if len(text) > 300:
                            out.append(f"[CONTEUDO de {links[0]}]: {text[:2200]}")
                    except Exception:
                        pass
                return {"used": ["duckduckgo"], "snippets": out}
        except Exception:
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
