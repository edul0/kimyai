from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifact_parser import parse_kemy_artifact


@dataclass
class BrowserReport:
    available: bool
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    console: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    viewports: list[dict[str, Any]] = field(default_factory=list)
    screenshot_data_url: str = ""

    def as_dict(self, include_screenshot: bool = False) -> dict[str, Any]:
        payload = {
            "available": self.available,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "console": self.console,
            "failed_requests": self.failed_requests,
            "viewports": self.viewports,
        }
        if include_screenshot:
            payload["screenshot_data_url"] = self.screenshot_data_url
        return payload

    def as_prompt(self) -> str:
        if not self.available:
            return "Navegador indisponivel: " + "; ".join(self.warnings)
        lines = [f"Browser QA: {'APROVADO' if self.ok else 'REPROVADO'}"]
        lines.extend(f"- ERRO: {item}" for item in self.errors)
        lines.extend(f"- AVISO: {item}" for item in self.warnings)
        return "\n".join(lines)


class FrontendBrowserRuntime:
    """Runs generated HTML in Playwright without granting filesystem mutation outside a temp copy."""

    VIEWPORTS = (("desktop", 1440, 900), ("mobile", 390, 844))

    async def inspect(self, raw: str) -> BrowserReport:
        artifact = parse_kemy_artifact(raw)
        if not artifact:
            return BrowserReport(False, False, warnings=["Artifact HTML ausente."])
        html_items = [item for item in artifact.files if item.path.lower().endswith(".html")]
        if not html_items:
            return BrowserReport(False, False, warnings=["Arquivo HTML ausente."])
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return BrowserReport(False, False, warnings=["Playwright nao instalado."])

        with tempfile.TemporaryDirectory(prefix="kemy-browser-") as temp:
            root = Path(temp).resolve()
            for item in artifact.files:
                target = (root / item.path.replace("\\", "/").lstrip("/")).resolve()
                if root not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item.content, encoding="utf-8")
            preferred = next((item for item in html_items if item.path.lower().endswith("preview.html")), html_items[0])
            page_url = (root / preferred.path.replace("\\", "/").lstrip("/")).resolve().as_uri()
            console: list[str] = []
            failed: list[str] = []
            errors: list[str] = []
            warnings: list[str] = []
            viewports: list[dict[str, Any]] = []
            screenshot = b""
            try:
                async with async_playwright() as playwright:
                    try:
                        browser = await playwright.chromium.launch(headless=True)
                    except Exception:
                        # Windows normally already has Edge; this avoids a separate browser download.
                        browser = await playwright.chromium.launch(headless=True, channel="msedge")
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.on("console", lambda message: console.append(f"{message.type}: {message.text}") if message.type in {"error", "warning"} else None)
                    page.on("pageerror", lambda error: errors.append(f"JavaScript: {error}"))
                    page.on("requestfailed", lambda request: failed.append(f"{request.method} {request.url}: {request.failure}"))
                    for name, width, height in self.VIEWPORTS:
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.goto(page_url, wait_until="networkidle", timeout=20_000)
                        await page.wait_for_timeout(250)
                        metrics = await page.evaluate(
                            """() => ({
                              bodyText: (document.body?.innerText || '').trim().length,
                              bodyWidth: document.body?.scrollWidth || 0,
                              viewportWidth: document.documentElement.clientWidth,
                              bodyHeight: document.body?.scrollHeight || 0,
                              visibleElements: [...document.querySelectorAll('body *')].filter(el => {
                                const s = getComputedStyle(el), r = el.getBoundingClientRect();
                                return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) > 0 && r.width > 0 && r.height > 0;
                              }).length
                            })"""
                        )
                        viewports.append({"name": name, "width": width, "height": height, **metrics})
                        if metrics["bodyText"] < 2 or metrics["visibleElements"] < 2:
                            errors.append(f"{name}: tela vazia ou sem elementos visiveis.")
                        if metrics["bodyWidth"] > metrics["viewportWidth"] + 4:
                            errors.append(f"{name}: overflow horizontal de {metrics['bodyWidth'] - metrics['viewportWidth']}px.")
                        if name == "desktop":
                            screenshot = await page.screenshot(full_page=True, type="png")

                    buttons = page.locator("button:visible, [role=button]:visible")
                    for index in range(min(await buttons.count(), 10)):
                        button = buttons.nth(index)
                        try:
                            await button.click(timeout=1500)
                            await page.wait_for_timeout(80)
                        except Exception:
                            warnings.append(f"Botao visivel #{index + 1} nao respondeu ao clique automatizado.")
                    await browser.close()
            except Exception as exc:
                return BrowserReport(
                    False, False,
                    warnings=[f"Chromium/Playwright indisponivel: {type(exc).__name__}: {str(exc)[:260]}"],
                )
            errors.extend(item for item in console if item.lower().startswith("error"))
            errors.extend(f"Requisicao falhou: {item}" for item in failed)
            data_url = f"data:image/png;base64,{base64.b64encode(screenshot).decode('ascii')}" if screenshot else ""
            return BrowserReport(
                available=True,
                ok=not errors,
                errors=list(dict.fromkeys(errors))[:30],
                warnings=list(dict.fromkeys(warnings))[:30],
                console=console[:50],
                failed_requests=failed[:30],
                viewports=viewports,
                screenshot_data_url=data_url,
            )
