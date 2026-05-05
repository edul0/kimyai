from __future__ import annotations

import base64
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import Settings


@dataclass
class Block:
    kind: str
    text: str
    level: int = 0
    rows: list[list[str]] | None = None


class DocumentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.output_root = Path("generated_documents")

    def generate(self, session_id: str, job_id: str, user_request: str, draft: dict[str, Any]) -> dict[str, Any]:
        source_text = self._source_text(user_request, draft)
        title = self._title_from_text(user_request, source_text)
        if self._is_slide_request(user_request, source_text):
            return self._generate_slides(session_id, job_id, user_request, title, source_text, draft)
        blocks = self._parse_blocks(source_text)
        folder = self.output_root / session_id / job_id
        folder.mkdir(parents=True, exist_ok=True)

        file_stem = self._filename_stem(user_request, title)
        docx_name = self._safe_filename(file_stem, ".docx")
        pdf_name = self._safe_filename(file_stem, ".pdf")
        docx_path = folder / docx_name
        pdf_path = folder / pdf_name

        self._build_docx(docx_path, title, blocks, user_request)
        pdf_provider = self._build_pdf(pdf_path, title, blocks, user_request, session_id)

        summary = "Documento organizado em DOCX e PDF gerado com sucesso."
        raw = (
            f"Arquivos gerados com sucesso: `{docx_name}` e `{pdf_name}`.\n\n"
            "Use os links do chat para abrir ou baixar o DOCX e o PDF."
        )
        return {
            "provider": draft.get("provider", "kimi-documento"),
            "model": f"{draft.get('model', 'kimi-documento')} + {pdf_provider}",
            "summary": summary,
            "raw": raw,
            "document_title": title,
            "files": [
                {
                    "name": docx_name,
                    "path": str(docx_path).replace("\\", "/"),
                    "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "download_url": f"/api/artefatos/{job_id}/{docx_name}",
                },
                {
                    "name": pdf_name,
                    "path": str(pdf_path).replace("\\", "/"),
                    "mime_type": "application/pdf",
                    "download_url": f"/api/artefatos/{job_id}/{pdf_name}",
                },
            ],
        }

    def _generate_slides(
        self,
        session_id: str,
        job_id: str,
        user_request: str,
        title: str,
        source_text: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        folder = self.output_root / session_id / job_id
        folder.mkdir(parents=True, exist_ok=True)

        file_stem = self._filename_stem(user_request, title)
        markdown_name = self._safe_filename(file_stem, ".md")
        html_name = self._safe_filename(file_stem, ".html")
        pdf_name = self._safe_filename(file_stem, ".pdf")
        markdown_path = folder / markdown_name
        html_path = folder / html_name
        pdf_path = folder / pdf_name

        raw_slides = self._split_raw_slides(source_text)
        slide_visuals = self._generate_slide_visuals(title, user_request, raw_slides, folder)
        marp_markdown = self._build_marp_markdown(title, source_text, slide_visuals=slide_visuals)
        markdown_path.write_text(marp_markdown, encoding="utf-8")

        html_created = self._render_marp_html(markdown_path, html_path)
        pdf_provider = self._render_slide_pdf(markdown_path, html_path, pdf_path)

        files = [
            {
                "name": markdown_name,
                "path": str(markdown_path).replace("\\", "/"),
                "mime_type": "text/markdown",
                "download_url": f"/api/artefatos/{job_id}/{markdown_name}",
            }
        ]
        if html_created and html_path.exists():
            files.append(
                {
                    "name": html_name,
                    "path": str(html_path).replace("\\", "/"),
                    "mime_type": "text/html",
                    "download_url": f"/api/artefatos/{job_id}/{html_name}",
                }
            )
        if pdf_path.exists():
            files.append(
                {
                    "name": pdf_name,
                    "path": str(pdf_path).replace("\\", "/"),
                    "mime_type": "application/pdf",
                    "download_url": f"/api/artefatos/{job_id}/{pdf_name}",
                }
            )

        summary = "Slides gerados em Markdown Marp e PDF."
        if slide_visuals:
            summary = "Slides gerados com composicao visual e imagens IA."
        raw = (
            f"Slides gerados com sucesso: `{markdown_name}`"
            + (f", `{html_name}`" if html_created and html_path.exists() else "")
            + (f" e `{pdf_name}`" if pdf_path.exists() else "")
            + "."
        )
        if slide_visuals:
            raw += f" Imagens IA aplicadas em {len(slide_visuals)} slide(s)."
        return {
            "provider": draft.get("provider", "kimi-slides"),
            "model": f"{draft.get('model', 'kimi-slides')} + {pdf_provider}",
            "summary": summary,
            "raw": raw,
            "document_title": title,
            "files": files,
            "slide_deck": True,
            "tools_used": ["marp-cli", *(["pollinations-image"] if slide_visuals else [])],
        }

    def _source_text(self, user_request: str, draft: dict[str, Any]) -> str:
        for key in ("raw", "summary"):
            value = str(draft.get(key) or "").strip()
            cleaned = self._normalize_document_text(self._strip_system_notices(value), user_request)
            if self._looks_like_document_content(cleaned):
                return cleaned
        return self._fallback_document_text(user_request)

    def _is_slide_request(self, user_request: str, text: str) -> bool:
        lowered = f"{user_request}\n{text}".lower()
        markers = [
            "slide",
            "slides",
            "apresentacao",
            "apresentação",
            "deck",
            "powerpoint",
            "ppt",
            "palestra",
        ]
        return any(marker in lowered for marker in markers)

    def _title_from_text(self, user_request: str, text: str) -> str:
        extracted = self._extract_topic(user_request)
        if extracted:
            return extracted[:80].rstrip(" .:-")
        for line in text.splitlines():
            cleaned = re.sub(r"^#+\s*", "", line).strip()
            if len(cleaned) >= 6:
                return cleaned[:80]
        base = " ".join(user_request.split()).strip()
        return (base[:80] or "Documento Kimi AI").rstrip(" .:-")

    def _build_marp_markdown(self, title: str, source_text: str, slide_visuals: dict[int, str] | None = None) -> str:
        cleaned = source_text.strip()
        slides = self._split_raw_slides(cleaned) if self._looks_like_marp_deck(cleaned) else self._split_into_slides(cleaned)
        slides = self._polish_slide_deck(slides, title, slide_visuals or {})
        frontmatter = (
            "---\n"
            "marp: true\n"
            "theme: gaia\n"
            "paginate: true\n"
            "size: 16:9\n"
            "headingDivider: 2\n"
            "footer: 'Kemy AI'\n"
            "style: |\n"
            "  section {\n"
            "    font-family: 'Aptos', 'Segoe UI', sans-serif;\n"
            "    background: linear-gradient(180deg, #f8fbff 0%, #eef3f9 100%);\n"
            "    color: #10243d;\n"
            "    padding: 54px 64px 58px;\n"
            "  }\n"
            "  section strong {\n"
            "    color: #0d315f;\n"
            "  }\n"
            "  section::after {\n"
            "    font-size: 0.72rem;\n"
            "    color: #68809f;\n"
            "  }\n"
            "  h1 {\n"
            "    color: #0b2e59;\n"
            "    font-size: 2.2rem;\n"
            "    line-height: 1.02;\n"
            "    letter-spacing: -0.03em;\n"
            "    margin: 0 0 0.32em;\n"
            "  }\n"
            "  h2 {\n"
            "    color: #0f437a;\n"
            "    font-size: 1.32rem;\n"
            "    line-height: 1.1;\n"
            "    letter-spacing: -0.02em;\n"
            "    margin: 0 0 0.5em;\n"
            "  }\n"
            "  h3 {\n"
            "    color: #1d5d96;\n"
            "    font-size: 1.0rem;\n"
            "    margin: 0 0 0.5em;\n"
            "  }\n"
            "  p {\n"
            "    font-size: 1rem;\n"
            "    line-height: 1.42;\n"
            "    margin: 0 0 0.7em;\n"
            "  }\n"
            "  ul, ol {\n"
            "    margin: 0.2em 0 0;\n"
            "    padding-left: 1.1em;\n"
            "  }\n"
            "  li {\n"
            "    font-size: 0.96rem;\n"
            "    line-height: 1.38;\n"
            "    margin: 0 0 0.34em;\n"
            "  }\n"
            "  strong {\n"
            "    color: #0b3264;\n"
            "    font-weight: 750;\n"
            "  }\n"
            "  blockquote {\n"
            "    margin: 0.8em 0;\n"
            "    padding: 0.7em 0.9em;\n"
            "    border-left: 5px solid #2f6fb1;\n"
            "    background: rgba(255,255,255,0.62);\n"
            "    border-radius: 0 12px 12px 0;\n"
            "  }\n"
            "  table {\n"
            "    width: 100%;\n"
            "    border-collapse: collapse;\n"
            "    margin-top: 0.5em;\n"
            "    font-size: 0.82rem;\n"
            "    background: rgba(255,255,255,0.84);\n"
            "    border-radius: 14px;\n"
            "    overflow: hidden;\n"
            "  }\n"
            "  th {\n"
            "    background: #0f437a;\n"
            "    color: #ffffff;\n"
            "    text-align: left;\n"
            "    padding: 10px 12px;\n"
            "  }\n"
            "  td {\n"
            "    padding: 10px 12px;\n"
            "    border-bottom: 1px solid rgba(15, 67, 122, 0.10);\n"
            "    vertical-align: top;\n"
            "  }\n"
            "  tr:last-child td {\n"
            "    border-bottom: 0;\n"
            "  }\n"
            "  code {\n"
            "    font-size: 0.82rem;\n"
            "    background: rgba(15, 67, 122, 0.08);\n"
            "    padding: 0.16em 0.34em;\n"
            "    border-radius: 6px;\n"
            "  }\n"
            "  hr {\n"
            "    border: 0;\n"
            "    height: 1px;\n"
            "    background: rgba(15, 67, 122, 0.18);\n"
            "  }\n"
            "  section.lead {\n"
            "    background: radial-gradient(circle at top left, rgba(99, 179, 237, 0.30), transparent 32%), linear-gradient(135deg, #0d1b2a 0%, #16324f 55%, #23486b 100%);\n"
            "    color: #f5fbff;\n"
            "    display: flex;\n"
            "    flex-direction: column;\n"
            "    justify-content: center;\n"
            "  }\n"
            "  section.lead h1, section.lead h2, section.lead strong, section.lead blockquote {\n"
            "    color: #ffffff;\n"
            "  }\n"
            "  section.lead p, section.lead li, section.lead::after {\n"
            "    color: rgba(255,255,255,0.88);\n"
            "  }\n"
            "  section.lead h1 {\n"
            "    font-size: 2.7rem;\n"
            "    max-width: 10.5em;\n"
            "    margin-bottom: 0.28em;\n"
            "  }\n"
            "  section.lead h2 {\n"
            "    font-size: 1.14rem;\n"
            "    font-weight: 500;\n"
            "    margin-bottom: 1em;\n"
            "    max-width: 30em;\n"
            "  }\n"
            "  section.lead blockquote {\n"
            "    background: rgba(255,255,255,0.08);\n"
            "    border-left-color: rgba(255,255,255,0.7);\n"
            "    max-width: 34em;\n"
            "  }\n"
            "  section.agenda ul {\n"
            "    display: grid;\n"
            "    grid-template-columns: 1fr 1fr;\n"
            "    gap: 0.55em 1.8em;\n"
            "    padding-left: 0;\n"
            "    list-style: none;\n"
            "    margin-top: 1.2em;\n"
            "  }\n"
            "  section.agenda li {\n"
            "    background: rgba(255,255,255,0.88);\n"
            "    border: 1px solid rgba(17, 55, 99, 0.08);\n"
            "    border-radius: 14px;\n"
            "    padding: 0.8em 0.95em;\n"
            "    box-shadow: 0 12px 28px rgba(17, 55, 99, 0.08);\n"
            "    margin: 0;\n"
            "  }\n"
            "  section.metrics .metrics-grid {\n"
            "    display: grid;\n"
            "    grid-template-columns: 1fr 1fr;\n"
            "    gap: 0.85em;\n"
            "    margin-top: 1.1em;\n"
            "  }\n"
            "  section.metrics .metric-card {\n"
            "    background: rgba(255,255,255,0.92);\n"
            "    border: 1px solid rgba(16, 36, 61, 0.08);\n"
            "    border-radius: 16px;\n"
            "    padding: 0.95em 1.05em;\n"
            "    box-shadow: 0 16px 30px rgba(23, 45, 78, 0.08);\n"
            "  }\n"
            "  section.metrics .metric-label {\n"
            "    display: block;\n"
            "    font-size: 0.78rem;\n"
            "    font-weight: 700;\n"
            "    letter-spacing: 0.04em;\n"
            "    text-transform: uppercase;\n"
            "    color: #55708f;\n"
            "    margin-bottom: 0.4em;\n"
            "  }\n"
            "  section.metrics .metric-value {\n"
            "    display: block;\n"
            "    font-size: 1.22rem;\n"
            "    font-weight: 800;\n"
            "    color: #10243d;\n"
            "    margin-bottom: 0.28em;\n"
            "  }\n"
            "  section.metrics .metric-text {\n"
            "    display: block;\n"
            "    font-size: 0.83rem;\n"
            "    line-height: 1.34;\n"
            "    color: #455d79;\n"
            "  }\n"
            "  section.timeline ol {\n"
            "    list-style: none;\n"
            "    counter-reset: steps;\n"
            "    margin-top: 1em;\n"
            "    padding-left: 0;\n"
            "  }\n"
            "  section.timeline li {\n"
            "    counter-increment: steps;\n"
            "    position: relative;\n"
            "    padding: 0.18em 0 0.9em 3em;\n"
            "    margin: 0;\n"
            "  }\n"
            "  section.timeline li::before {\n"
            "    content: counter(steps);\n"
            "    position: absolute;\n"
            "    left: 0;\n"
            "    top: 0.06em;\n"
            "    width: 2em;\n"
            "    height: 2em;\n"
            "    border-radius: 999px;\n"
            "    background: #163f73;\n"
            "    color: #fff;\n"
            "    display: grid;\n"
            "    place-items: center;\n"
            "    font-size: 0.75rem;\n"
            "    font-weight: 800;\n"
            "  }\n"
            "  section.timeline li::after {\n"
            "    content: '';\n"
            "    position: absolute;\n"
            "    left: 0.95em;\n"
            "    top: 2.2em;\n"
            "    bottom: -0.1em;\n"
            "    width: 2px;\n"
            "    background: rgba(22, 63, 115, 0.16);\n"
            "  }\n"
            "  section.timeline li:last-child::after {\n"
            "    display: none;\n"
            "  }\n"
            "  section.compare table {\n"
            "    margin-top: 1.1em;\n"
            "    box-shadow: 0 16px 32px rgba(23, 45, 78, 0.08);\n"
            "  }\n"
            "  section.closing {\n"
            "    background: linear-gradient(135deg, #10243d 0%, #1c4f88 100%);\n"
            "    color: #f4f8fc;\n"
            "    display: flex;\n"
            "    flex-direction: column;\n"
            "    justify-content: center;\n"
            "  }\n"
            "  section.closing h1, section.closing h2, section.closing strong {\n"
            "    color: #ffffff;\n"
            "  }\n"
            "  section.closing p, section.closing li, section.closing::after {\n"
            "    color: rgba(255,255,255,0.9);\n"
            "  }\n"
            "  section.closing ul {\n"
            "    margin-top: 1em;\n"
            "  }\n"
            "---\n\n"
        )
        return frontmatter + "\n\n---\n\n".join(slides).strip() + "\n"

    def _split_raw_slides(self, text: str) -> list[str]:
        return [chunk.strip() for chunk in re.split(r"\n---+\n", text) if chunk.strip()]

    def _looks_like_marp_deck(self, text: str) -> bool:
        if "marp: true" in text.lower():
            return True
        return text.count("\n---") >= 1 and ("# " in text or "## " in text)

    def _split_into_slides(self, text: str) -> list[str]:
        if "\n---" in text:
            parts = [chunk.strip() for chunk in re.split(r"\n---+\n", text) if chunk.strip()]
            if parts:
                return parts
        sections = []
        current: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^#{1,2}\s+", stripped) and current:
                sections.append("\n".join(current).strip())
                current = [stripped]
            else:
                current.append(stripped)
        if current:
            sections.append("\n".join(current).strip())
        sections = [section for section in sections if section]
        if len(sections) <= 1:
            return [f"# {self._slide_title_from_text(text)}\n\n{self._slide_bullets(text)}"]
        normalized = [self._normalize_slide(section) for section in sections]
        if normalized:
            normalized[0] = self._upgrade_cover_slide(normalized[0])
        return normalized

    def _polish_slide_deck(self, slides: list[str], title: str, slide_visuals: dict[int, str]) -> list[str]:
        polished: list[str] = []
        total = len(slides)
        for index, slide in enumerate(slides):
            working = slide.strip()
            slide_class = self._infer_slide_class(working, index, total)
            if slide_class == "lead":
                working = self._upgrade_cover_slide(working)
            elif slide_class == "agenda":
                working = self._normalize_agenda_slide(working)
            elif slide_class == "metrics":
                working = self._render_metrics_slide(working)
            elif slide_class == "timeline":
                working = self._render_timeline_slide(working)
            elif slide_class == "closing":
                working = self._render_closing_slide(working, title)
            visual_path = slide_visuals.get(index)
            if visual_path:
                working = self._inject_visual_asset(working, visual_path, slide_class)
            working = self._inject_slide_class(working, slide_class)
            polished.append(working)
        return polished

    def _generate_slide_visuals(
        self,
        title: str,
        user_request: str,
        slides: list[str],
        folder: Path,
    ) -> dict[int, str]:
        if not self.settings.pollinations_api_key or not slides:
            return {}
        selected_indexes = self._select_visual_slides(slides)
        if not selected_indexes:
            return {}
        assets_dir = folder / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        visuals: dict[int, str] = {}
        for slide_index in selected_indexes:
            prompt = self._slide_image_prompt(title, user_request, slides[slide_index], slide_index, len(slides))
            output_path = assets_dir / f"slide-{slide_index + 1:02d}-visual.png"
            try:
                self._download_pollinations_image(prompt, output_path)
                visuals[slide_index] = output_path.name
            except Exception:
                continue
        return visuals

    def _select_visual_slides(self, slides: list[str]) -> list[int]:
        choices: list[int] = []
        if slides:
            choices.append(0)
        for idx, slide in enumerate(slides[1:-1], start=1):
            lower = slide.lower()
            if any(token in lower for token in ["impacto", "cenario", "cenario", "mercado", "processo", "cronograma", "arquitetura", "roadmap", "estrategia", "estratégia"]):
                choices.append(idx)
            if len(choices) >= 3:
                break
        if len(choices) < 2 and len(slides) > 2:
            choices.append(1)
        deduped: list[int] = []
        for item in choices:
            if item not in deduped:
                deduped.append(item)
        return deduped[:3]

    def _slide_image_prompt(
        self,
        deck_title: str,
        user_request: str,
        slide: str,
        slide_index: int,
        total_slides: int,
    ) -> str:
        lines = [line.strip() for line in slide.splitlines() if line.strip() and not line.strip().startswith("<!--")]
        heading = re.sub(r"^#{1,3}\s*", "", lines[0]).strip() if lines else deck_title
        bullets = [
            re.sub(r"^[-*]\s+", "", line).strip()
            for line in lines[1:]
            if re.match(r"^[-*]\s+", line)
        ][:3]
        narrative = "; ".join(bullets)[:360]
        is_cover = slide_index == 0
        if is_cover:
            return (
                f"Premium presentation cover image for '{deck_title}'. "
                f"Theme: {heading}. "
                f"Editorial, cinematic, polished corporate storytelling, high-end consulting deck aesthetic, "
                f"clean composition with negative space for title text, subtle depth, modern lighting, no text, no watermark. "
                f"Context: {user_request[:260]}"
            )
        return (
            f"Presentation visual for slide {slide_index + 1} of {total_slides} about '{deck_title}'. "
            f"Slide topic: {heading}. "
            f"Key points: {narrative or user_request[:220]}. "
            "Professional editorial illustration or photoreal concept for a boardroom-grade presentation, "
            "clean composition, sophisticated color palette, suitable for split-slide layout, no text, no watermark."
        )

    def _download_pollinations_image(self, prompt: str, output_path: Path) -> None:
        headers = {"Authorization": f"Bearer {self.settings.pollinations_api_key}"}
        payload = {
            "model": self.settings.pollinations_image_model,
            "prompt": prompt,
            "size": "1536x1024",
            "quality": self.settings.pollinations_image_quality,
            "response_format": "b64_json",
        }
        with httpx.Client(timeout=150) as client:
            response = client.post("https://gen.pollinations.ai/v1/images/generations", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        image_payload = (data.get("data") or [{}])[0]
        image_b64 = image_payload.get("b64_json")
        image_url = image_payload.get("url")
        if image_b64:
            output_path.write_bytes(base64.b64decode(image_b64))
            return
        if image_url:
            with httpx.Client(timeout=150) as client:
                response = client.get(image_url)
                response.raise_for_status()
            output_path.write_bytes(response.content)
            return
        raise RuntimeError("Imagem de slide nao retornou conteudo utilizavel.")

    def _inject_visual_asset(self, slide: str, visual_path: str, slide_class: str) -> str:
        normalized = visual_path.replace("\\", "/")
        if slide_class == "lead":
            visual_line = f"![bg right:42%]({quote(normalized, safe='/:.-_')})"
        else:
            visual_line = f"![bg right:36%]({quote(normalized, safe='/:.-_')})"
        return f"{visual_line}\n\n{slide.strip()}"

    def _infer_slide_class(self, slide: str, index: int, total: int) -> str:
        lower = slide.lower()
        if index == 0:
            return "lead"
        if index == total - 1:
            return "closing"
        if "|" in slide and "\n" in slide:
            return "compare"
        if any(token in lower for token in ["agenda", "roteiro", "sumario", "sumário", "visao geral", "visão geral"]):
            return "agenda"
        if any(token in lower for token in ["roadmap", "cronograma", "etapas", "fases", "processo", "passos", "plano"]):
            return "timeline"
        bullet_count = len(re.findall(r"(?m)^[-*]\s+", slide))
        numeric_hits = len(re.findall(r"\b\d+(?:[%x]|(?:[.,]\d+)?)\b", slide))
        if bullet_count >= 3 and numeric_hits >= 2:
            return "metrics"
        return "section"

    def _inject_slide_class(self, slide: str, slide_class: str) -> str:
        directive = f"<!-- _class: {slide_class} -->"
        if slide.lstrip().startswith("<!-- _class:"):
            return slide
        return f"{directive}\n\n{slide.strip()}"

    def _normalize_agenda_slide(self, slide: str) -> str:
        lines = [line.strip() for line in slide.splitlines() if line.strip()]
        if not lines:
            return slide
        heading = lines[0]
        body = []
        for line in lines[1:]:
            if re.match(r"^[-*]\s+", line):
                body.append(line)
            elif not line.startswith(">"):
                body.append(f"- {line}")
        if not body:
            return slide
        return "\n".join([heading, ""] + body)

    def _render_metrics_slide(self, slide: str) -> str:
        title, bullets, extras = self._split_slide_content(slide)
        cards: list[str] = []
        for bullet in bullets[:4]:
            label, value, text = self._parse_metric_bullet(bullet)
            cards.append(
                '<div class="metric-card">'
                f'<span class="metric-label">{self._escape_html(label)}</span>'
                f'<span class="metric-value">{self._escape_html(value)}</span>'
                f'<span class="metric-text">{self._escape_html(text)}</span>'
                "</div>"
            )
        html_block = '<div class="metrics-grid">' + "".join(cards) + "</div>"
        parts = [title, "", html_block]
        if extras:
            parts.extend([""] + extras)
        return "\n".join(parts)

    def _render_timeline_slide(self, slide: str) -> str:
        title, bullets, extras = self._split_slide_content(slide)
        if not bullets:
            return slide
        ordered = [re.sub(r"^[-*]\s+", "", bullet).strip() for bullet in bullets[:5]]
        body = [title, "", "<ol>"] + [f"<li>{self._escape_html(item)}</li>" for item in ordered] + ["</ol>"]
        if extras:
            body.extend([""] + extras)
        return "\n".join(body)

    def _render_closing_slide(self, slide: str, title: str) -> str:
        lines = [line.strip() for line in slide.splitlines() if line.strip()]
        if not lines:
            return slide
        if len(lines) == 1:
            return f"# {title}\n\n## Recomendacao final\n\n- Priorize execucao simples, narrativa clara e proximos passos concretos."
        if not any(line.startswith("## ") for line in lines[1:]):
            lines.insert(1, "## Recomendacao final")
        return "\n".join(lines)

    def _split_slide_content(self, slide: str) -> tuple[str, list[str], list[str]]:
        lines = [line.strip() for line in slide.splitlines() if line.strip() and not line.strip().startswith("<!--")]
        if not lines:
            return "## Slide", [], []
        title = lines[0]
        bullets = [line for line in lines[1:] if re.match(r"^[-*]\s+", line)]
        extras = [line for line in lines[1:] if line not in bullets]
        return title, bullets, extras

    def _parse_metric_bullet(self, bullet: str) -> tuple[str, str, str]:
        content = re.sub(r"^[-*]\s+", "", bullet).strip()
        strong_match = re.match(r"^\*\*(.+?)\*\*\s*[-:]\s*(.+)$", content)
        if strong_match:
            label = strong_match.group(1).strip()
            tail = strong_match.group(2).strip()
        else:
            parts = re.split(r"\s[-:]\s", content, maxsplit=1)
            label = parts[0].strip()
            tail = parts[1].strip() if len(parts) > 1 else ""
        value_match = re.match(r"^([0-9][^,;.]{0,24})(?:\s*[,-]\s*|\s+)(.+)$", tail)
        if value_match:
            value = value_match.group(1).strip()
            text = value_match.group(2).strip()
        else:
            tokens = tail.split()
            value = tokens[0] if tokens else label
            text = " ".join(tokens[1:]).strip() or tail or "Indicador-chave da narrativa."
        return label or "Indicador", value, text

    def _slide_title_from_text(self, text: str) -> str:
        for line in text.splitlines():
            cleaned = re.sub(r"^#{1,3}\s*", "", line).strip()
            if cleaned:
                return cleaned[:80]
        return "Apresentacao"

    def _normalize_slide(self, section: str) -> str:
        lines = [line.rstrip() for line in section.splitlines() if line.strip()]
        if not lines:
            return "## Slide\n\n- Conteudo"
        if not re.match(r"^#{1,2}\s+", lines[0]):
            lines.insert(0, "## Slide")
        return "\n".join(lines)

    def _upgrade_cover_slide(self, slide: str) -> str:
        lines = [line for line in slide.splitlines() if line.strip()]
        if not lines:
            return slide
        title = re.sub(r"^#{1,2}\s+", "", lines[0]).strip()
        body = [line for line in lines[1:] if line.strip()]
        subtitle = body[0] if body else "Visao geral executiva"
        return (
            f"# {title}\n\n"
            f"## {subtitle}\n\n"
            "> Panorama claro, visual e pronto para apresentar.\n"
        )

    def _slide_bullets(self, text: str) -> str:
        chunks = [chunk.strip() for chunk in re.split(r"(?<=[\.\!\?])\s+", " ".join(text.split())) if chunk.strip()]
        bullets = [f"- {chunk}" for chunk in chunks[:5]]
        return "\n".join(bullets) or "- Conteudo principal"

    def _render_marp_html(self, markdown_path: Path, html_path: Path) -> bool:
        command = self._marp_command(markdown_path, html_path, "--html")
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            return True
        except Exception:
            return False

    def _render_slide_pdf(self, markdown_path: Path, html_path: Path, pdf_path: Path) -> str:
        if self.settings.gotenberg_url and html_path.exists():
            try:
                self._build_pdf_with_gotenberg(pdf_path, markdown_path.stem, html_path.read_text(encoding="utf-8"))
                return "marp-html + gotenberg"
            except Exception:
                pass
        command = self._marp_command(markdown_path, pdf_path, "--pdf")
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
            return "marp-cli"
        except Exception:
            return "marp-markdown"

    def _marp_command(self, input_path: Path, output_path: Path, mode: str) -> list[str]:
        marp_bin = shutil.which("marp")
        if marp_bin:
            return [marp_bin, str(input_path), mode, "-o", str(output_path), "--allow-local-files"]
        npx_bin = shutil.which("npx") or shutil.which("npx.cmd")
        if npx_bin:
            return [npx_bin, "--yes", "@marp-team/marp-cli", str(input_path), mode, "-o", str(output_path), "--allow-local-files"]
        raise FileNotFoundError("Marp CLI nao encontrado.")

    def _filename_stem(self, user_request: str, title: str) -> str:
        request_text = " ".join(user_request.split()).strip(" .:-")
        if request_text:
            return request_text[:120]
        return title

    def _safe_filename(self, title: str, extension: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
        return f"{slug or 'documento-kimi-ai'}{extension}"

    def _normalize_document_text(self, text: str, user_request: str) -> str:
        cleaned = text.strip()
        if not cleaned:
            return cleaned
        cleaned = cleaned.replace("\u25a0", " ")
        cleaned = cleaned.replace("\u00a0", " ")
        cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", cleaned)
        cleaned = re.sub(r"^```(?:markdown|md|text)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = re.sub(
            r"(?is)^#+\s*pedido:.*?(?=^#+\s+|^[A-ZÁÀÂÃÉÈÊÍÌÎÓÒÔÕÚÙÛÇa-záàâãéèêíìîóòôõúùûç].*$|$)",
            "",
            cleaned,
        ).strip()
        lead_patterns = [
            r"(?is)^compreendido\.?\s+voc[eê].{0,220}?(?=\n\s*\n|^#|\Z)",
            r"(?is)^aqui est[aá].{0,220}?(?=\n\s*\n|^#|\Z)",
            r"(?is)^abaixo est[aá].{0,220}?(?=\n\s*\n|^#|\Z)",
            r"(?is)^segue.{0,180}?(?=\n\s*\n|^#|\Z)",
            r"(?is)^este documento.{0,220}?(?=\n\s*\n|^#|\Z)",
            r"(?is)^o documento a seguir.{0,220}?(?=\n\s*\n|^#|\Z)",
        ]
        for pattern in lead_patterns:
            cleaned = re.sub(pattern, "", cleaned).strip()
        request_topic = self._extract_topic(user_request).lower()
        cleaned = re.sub(
            r"(?is)^pedido original:\s*.*?(?=\n\s*\n|^#|\Z)",
            "",
            cleaned,
        ).strip()
        if request_topic:
            cleaned = re.sub(
                r"(?is)^int[eê]n[cç][aã]o:.*?(?=\n\s*\n|^#|\Z)",
                "",
                cleaned,
            ).strip()
        return cleaned

    def _strip_system_notices(self, text: str) -> str:
        lines = [line.rstrip() for line in text.splitlines()]
        cleaned: list[str] = []
        skipping_notice = False
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith("aviso de sistema:"):
                skipping_notice = True
                continue
            if skipping_notice and not stripped:
                skipping_notice = False
                continue
            if skipping_notice and re.search(r"\b(gemini|groq|cerebras|openrouter|fallback|indisponivel|indisponível)\b", stripped, re.I):
                continue
            cleaned.append(line)
        result = "\n".join(cleaned).strip()
        return result or text.strip()

    def _looks_like_document_content(self, text: str) -> bool:
        if not text:
            return False
        compact = " ".join(text.split())
        if len(compact) < 120:
            return False
        lowered = compact.lower()
        banned_markers = [
            "aviso de sistema:",
            "fallback ativo",
            "indisponivel para esta chave",
            "indisponível para esta chave",
            "classificacao interna",
            "intenção:",
            "intencao:",
        ]
        if any(marker in lowered for marker in banned_markers):
            return False
        return True

    def _fallback_document_text(self, user_request: str) -> str:
        topic = self._extract_topic(user_request)
        return (
            f"# {topic}\n\n"
            "## Resumo executivo\n"
            f"Este documento apresenta uma visao organizada sobre {topic}, com foco em contexto, principais acontecimentos, impactos e pontos de atencao.\n\n"
            "## Contexto geral\n"
            f"{topic} deve ser entendido a partir do seu contexto historico, politico, social e tecnologico. "
            "O objetivo aqui e reunir uma base clara para leitura, estudo ou apresentacao.\n\n"
            "## Principais pontos\n"
            f"- Origem e antecedentes relacionados a {topic}\n"
            f"- Eventos, marcos ou fases centrais de {topic}\n"
            f"- Consequencias diretas e indiretas de {topic}\n"
            f"- Leitura critica sobre os impactos de {topic}\n\n"
            "## Desenvolvimento\n"
            f"A analise de {topic} pode ser organizada em uma linha do tempo, nos atores envolvidos e nos efeitos produzidos ao longo do tempo. "
            "Em um documento final, essa secao serve para aprofundar os fatos e organizar a narrativa de forma legivel.\n\n"
            "## Impactos e interpretacoes\n"
            f"Os impactos de {topic} podem incluir transformacoes politicas, economicas, culturais, sociais ou estrategicas. "
            "Tambem e importante destacar controversias, diferentes interpretacoes e consequencias de longo prazo.\n\n"
            "## Conclusao\n"
            f"{topic} e um tema que exige organizacao clara, criterio e boa hierarquia textual. "
            "Este PDF e DOCX foram estruturados para servir como ponto de partida para leitura, estudo e refinamento posterior.\n"
        )

    def _extract_topic(self, user_request: str) -> str:
        text = " ".join(user_request.split()).strip()
        text = re.sub(r"^(me\s+)?(gere|gerar|gera|crie|criar|fa[cç]a|fazer|monte|montar|produza|produzir)\s+", "", text, flags=re.I)
        text = re.sub(r"^(um|uma|o|a)\s+", "", text, flags=re.I)
        text = re.sub(r"\b(pdf|docx|arquivo|documento|word|markdown)\b", "", text, flags=re.I)
        text = re.sub(r"\s{2,}", " ", text).strip(" .:-")
        patterns = [
            r"(?:sobre|do|da|de)\s+(.+)$",
            r"(?:pedido|tema)\s+(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                topic = match.group(1).strip(" .:-")
                if topic:
                    return topic[:90].capitalize()
        return (text[:90] or "Documento solicitado").capitalize()

    def _parse_blocks(self, text: str) -> list[Block]:
        blocks: list[Block] = []
        bullet_buffer: list[str] = []
        numbered_buffer: list[str] = []
        table_buffer: list[str] = []

        def flush_buffers() -> None:
            nonlocal bullet_buffer, numbered_buffer, table_buffer
            if table_buffer:
                rows = self._parse_markdown_table(table_buffer)
                if rows:
                    blocks.append(Block("table", "", rows=rows))
                else:
                    for item in table_buffer:
                        if item.strip():
                            blocks.append(Block("paragraph", self._clean_inline_markdown(item.strip())))
            for item in bullet_buffer:
                blocks.append(Block("bullet", self._clean_inline_markdown(item)))
            for item in numbered_buffer:
                blocks.append(Block("numbered", self._clean_inline_markdown(item)))
            table_buffer = []
            bullet_buffer = []
            numbered_buffer = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_buffers()
                continue
            if line.startswith("```"):
                continue
            if re.fullmatch(r"-{3,}", line):
                continue
            if self._looks_like_table_line(line):
                table_buffer.append(line)
                continue
            if table_buffer:
                flush_buffers()
            heading = re.match(r"^(#{1,3})\s+(.*)$", line)
            if heading:
                flush_buffers()
                blocks.append(Block("heading", self._clean_inline_markdown(heading.group(2).strip()), len(heading.group(1))))
                continue
            bullet = re.match(r"^[-*]\s+(.*)$", line)
            if bullet:
                bullet_buffer.append(bullet.group(1).strip())
                continue
            numbered = re.match(r"^\d+[.)]\s+(.*)$", line)
            if numbered:
                numbered_buffer.append(numbered.group(1).strip())
                continue
            flush_buffers()
            blocks.append(Block("paragraph", self._clean_inline_markdown(line)))

        flush_buffers()
        if not blocks:
            blocks.append(Block("paragraph", text.strip() or "Documento sem conteudo."))
        return blocks

    def _build_docx(self, path: Path, title: str, blocks: list[Block], user_request: str) -> None:
        doc = Document()
        section = doc.sections[0]
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

        self._configure_styles(doc)
        self._configure_header(section, title)
        self._configure_footer(section)

        doc.add_paragraph(title, style="Title")
        doc.add_paragraph()

        for block in blocks:
            if block.kind == "heading":
                style_name = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}.get(block.level, "Heading 2")
                doc.add_paragraph(block.text, style=style_name)
            elif block.kind == "table" and block.rows:
                self._append_docx_table(doc, block.rows)
            elif block.kind == "bullet":
                doc.add_paragraph(block.text, style="List Bullet")
            elif block.kind == "numbered":
                doc.add_paragraph(block.text, style="List Number")
            else:
                doc.add_paragraph(block.text, style="Normal")

        doc.save(path)

    def _build_pdf(self, path: Path, title: str, blocks: list[Block], user_request: str, session_id: str) -> str:
        html = self._build_html(title, blocks, user_request, session_id)
        if self.settings.gotenberg_url:
            try:
                self._build_pdf_with_gotenberg(path, title, html)
                return "gotenberg"
            except Exception:
                pass
        try:
            self._build_pdf_with_playwright(path, html)
            return "playwright"
        except Exception:
            pass

        self._build_pdf_with_reportlab(path, title, blocks, user_request)
        return "reportlab"

    def _build_pdf_with_reportlab(self, path: Path, title: str, blocks: list[Block], user_request: str) -> None:
        styles = getSampleStyleSheet()
        body = ParagraphStyle(
            "KimiBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=11,
            leading=16,
            spaceAfter=10,
            textColor=colors.HexColor("#243240"),
        )
        body.wordWrap = "CJK"
        h1 = ParagraphStyle("KimiH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=colors.HexColor("#14213d"), spaceBefore=10, spaceAfter=8)
        h2 = ParagraphStyle("KimiH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=colors.HexColor("#223b63"), spaceBefore=8, spaceAfter=6)
        title_style = ParagraphStyle("KimiTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=colors.HexColor("#0f172a"), spaceAfter=8)
        table_cell = ParagraphStyle("KimiTableCell", parent=body, fontSize=10, leading=14, spaceAfter=0)
        table_header = ParagraphStyle("KimiTableHeader", parent=table_cell, fontName="Helvetica-Bold", textColor=colors.white)
        story: list[Any] = [Paragraph(title, title_style), Spacer(1, 0.08 * inch)]

        bullet_items: list[ListItem] = []
        numbered_items: list[ListItem] = []

        def flush_lists() -> None:
            nonlocal bullet_items, numbered_items
            if bullet_items:
                story.append(ListFlowable(bullet_items, bulletType="bullet", leftIndent=18))
                story.append(Spacer(1, 0.08 * inch))
                bullet_items = []
            if numbered_items:
                story.append(ListFlowable(numbered_items, bulletType="1", leftIndent=18))
                story.append(Spacer(1, 0.08 * inch))
                numbered_items = []

        for block in blocks:
            if block.kind == "bullet":
                bullet_items.append(ListItem(Paragraph(block.text, body)))
                continue
            if block.kind == "numbered":
                numbered_items.append(ListItem(Paragraph(block.text, body)))
                continue
            flush_lists()
            if block.kind == "heading":
                story.append(Paragraph(block.text, h1 if block.level == 1 else h2))
            elif block.kind == "table" and block.rows:
                story.append(self._build_pdf_table(block.rows, table_header, table_cell))
                story.append(Spacer(1, 0.12 * inch))
            else:
                story.append(Paragraph(block.text, body))
        flush_lists()

        pdf = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch)
        pdf.build(story)

    def _build_pdf_with_gotenberg(self, path: Path, title: str, html: str) -> None:
        base_url = (self.settings.gotenberg_url or "").rstrip("/")
        if not base_url:
            raise ValueError("Gotenberg URL nao configurada.")
        if "://" not in base_url:
            base_url = f"http://{base_url}"
        headers = {"Gotenberg-Output-Filename": self._safe_filename(title, "").strip(".-") or "documento-kimi-ai"}
        data = {
            "printBackground": "true",
            "generateDocumentOutline": "true",
        }
        files = [("files", ("index.html", html.encode("utf-8"), "text/html; charset=utf-8"))]
        with httpx.Client(timeout=self.settings.gotenberg_timeout_seconds) as client:
            response = client.post(f"{base_url}/forms/chromium/convert/html", headers=headers, data=data, files=files)
            response.raise_for_status()
        path.write_bytes(response.content)

    def _build_pdf_with_playwright(self, path: Path, html: str) -> None:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page()
                page.set_content(html, wait_until="networkidle")
                page.pdf(
                    path=str(path),
                    format="A4",
                    print_background=True,
                    margin={
                        "top": "24mm",
                        "right": "18mm",
                        "bottom": "22mm",
                        "left": "18mm",
                    },
                )
                browser.close()
        except PlaywrightError as exc:
            raise RuntimeError("Falha ao gerar PDF com Playwright.") from exc

    def _build_html(self, title: str, blocks: list[Block], user_request: str, session_id: str) -> str:
        content_parts = []
        open_list: str | None = None
        for block in blocks:
            if block.kind in {"bullet", "numbered"}:
                tag = "ul" if block.kind == "bullet" else "ol"
                if open_list != tag:
                    if open_list:
                        content_parts.append(f"</{open_list}>")
                    content_parts.append(f"<{tag}>")
                    open_list = tag
                content_parts.append(f"<li>{self._escape_html(block.text)}</li>")
                continue
            if open_list:
                content_parts.append(f"</{open_list}>")
                open_list = None
            if block.kind == "heading":
                level = min(max(block.level, 1), 3)
                content_parts.append(f"<h{level}>{self._escape_html(block.text)}</h{level}>")
            elif block.kind == "table" and block.rows:
                content_parts.append(self._build_html_table(block.rows))
            else:
                content_parts.append(f"<p>{self._escape_html(block.text)}</p>")
        if open_list:
            content_parts.append(f"</{open_list}>")

        return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>{self._escape_html(title)}</title>
  <style>
    @page {{
      size: A4;
      margin: 24mm 18mm 22mm 18mm;
    }}
    body {{
      font-family: Arial, Helvetica, sans-serif;
      color: #243240;
      margin: 0;
      font-size: 12px;
      line-height: 1.6;
      background: #ffffff;
    }}
    header {{
      margin-bottom: 20px;
    }}
    h1 {{
      font-size: 28px;
      line-height: 1.15;
      margin: 8px 0 6px;
      color: #0f172a;
    }}
    h2 {{
      font-size: 18px;
      margin: 24px 0 8px;
      color: #14213d;
    }}
    h3 {{
      font-size: 15px;
      margin: 18px 0 6px;
      color: #223b63;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin: 0 0 14px;
      table-layout: fixed;
    }}
    th, td {{
      border: 1px solid #c8d4e3;
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #1f4b8f;
      color: #ffffff;
      font-weight: 700;
    }}
    tbody tr:nth-child(even) {{
      background: #f4f7fb;
    }}
    p {{
      margin: 0 0 10px;
    }}
    ul, ol {{
      margin: 0 0 12px 22px;
      padding: 0;
    }}
    li {{
      margin: 0 0 8px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{self._escape_html(title)}</h1>
  </header>
  {''.join(content_parts)}
</body>
</html>"""

    def _escape_html(self, value: str) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _clean_inline_markdown(self, text: str) -> str:
        cleaned = str(text or "").replace("\u25a0", " ")
        cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", cleaned)
        cleaned = cleaned.replace("\\|", "|")
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", cleaned)
        cleaned = re.sub(r"\s+\|\s*$", "", cleaned)
        cleaned = re.sub(r"^\|\s*", "", cleaned)
        return " ".join(cleaned.split()).strip()

    def _looks_like_table_line(self, line: str) -> bool:
        stripped = line.strip()
        if stripped.count("|") < 2:
            return False
        if stripped.startswith("|") or stripped.endswith("|"):
            return True
        return bool(re.match(r"^[^|]+\|[^|]+\|", stripped))

    def _parse_markdown_table(self, lines: list[str]) -> list[list[str]]:
        rows: list[list[str]] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            cells = [self._clean_inline_markdown(cell) for cell in stripped.strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
                continue
            if any(cells):
                rows.append(cells)
        if len(rows) < 2:
            return []
        width = max(len(row) for row in rows)
        return [row + [""] * (width - len(row)) for row in rows]

    def _append_docx_table(self, doc: Document, rows: list[list[str]]) -> None:
        table = doc.add_table(rows=len(rows), cols=len(rows[0]))
        table.style = "Table Grid"
        table.autofit = True
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                cell = table.cell(row_index, col_index)
                cell.text = value
                if row_index == 0 and cell.paragraphs:
                    for run in cell.paragraphs[0].runs:
                        run.font.bold = True
        doc.add_paragraph()

    def _build_pdf_table(self, rows: list[list[str]], header_style: ParagraphStyle, cell_style: ParagraphStyle) -> Table:
        rendered = []
        for row_index, row in enumerate(rows):
            rendered.append(
                [
                    Paragraph(self._escape_reportlab(value), header_style if row_index == 0 else cell_style)
                    for value in row
                ]
            )
        table = Table(rendered, repeatRows=1, hAlign="LEFT")
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4b8f")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f8fafc")),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#ffffff"), colors.HexColor("#f4f7fb")]),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8d4e3")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        return table

    def _build_html_table(self, rows: list[list[str]]) -> str:
        header = "".join(f"<th>{self._escape_html(cell)}</th>" for cell in rows[0])
        body_rows = []
        for row in rows[1:]:
            body_rows.append("<tr>" + "".join(f"<td>{self._escape_html(cell)}</td>" for cell in row) + "</tr>")
        return (
            "<table><thead><tr>"
            + header
            + "</tr></thead><tbody>"
            + "".join(body_rows)
            + "</tbody></table>"
        )

    def _escape_reportlab(self, value: str) -> str:
        return self._escape_html(value).replace("\n", "<br/>")

    def _configure_styles(self, doc: Document) -> None:
        normal = doc.styles["Normal"]
        normal.font.name = "Arial"
        normal.font.size = Pt(11)

        title = doc.styles["Title"]
        title.font.name = "Arial"
        title.font.size = Pt(22)
        title.font.bold = True
        title.font.color.rgb = RGBColor(15, 23, 42)

        if "Subtitle" in doc.styles:
            subtitle = doc.styles["Subtitle"]
            subtitle.font.name = "Arial"
            subtitle.font.size = Pt(11)
            subtitle.font.color.rgb = RGBColor(91, 100, 114)

        for style_name, size in (("Heading 1", 16), ("Heading 2", 14), ("Heading 3", 12)):
            style = doc.styles[style_name]
            style.font.name = "Arial"
            style.font.size = Pt(size)
            style.font.bold = True
            style.font.color.rgb = RGBColor(20, 33, 61)

    def _configure_header(self, section: Any, label: str) -> None:
        paragraph = section.header.paragraphs[0]
        run = paragraph.add_run(label)
        run.font.name = "Arial"
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(91, 100, 114)
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "4")
        bottom.set(qn("w:color"), "D7DDE5")
        border.append(bottom)
        paragraph._p.get_or_add_pPr().append(border)

    def _configure_footer(self, section: Any) -> None:
        paragraph = section.footer.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        run = paragraph.add_run("Kimi AI")
        run.font.name = "Arial"
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(91, 100, 114)
