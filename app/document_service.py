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
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import Settings


@dataclass
class Block:
    kind: str
    text: str
    level: int = 0
    rows: list[list[str]] | None = None


@dataclass
class Slide:
    title: str
    kicker: str = ""
    body: list[str] | None = None
    bullets: list[str] | None = None
    rows: list[list[str]] | None = None
    layout: str = "content"
    visual: str | None = None


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

        normalized_source = self._normalize_slide_source(source_text)
        if self._needs_slide_fallback(normalized_source, user_request):
            normalized_source = self._fallback_slide_deck_text(user_request, title)
        raw_slides = self._split_raw_slides(normalized_source) if self._looks_like_marp_deck(normalized_source.strip()) else self._split_into_slides(normalized_source.strip())
        if len(raw_slides) < 6:
            normalized_source = self._fallback_slide_deck_text(user_request, title)
            raw_slides = self._split_into_slides(normalized_source)
        slide_visuals = self._generate_slide_visuals(title, user_request, raw_slides, folder)
        marp_markdown = self._build_marp_markdown(title, normalized_source, slide_visuals=slide_visuals)
        markdown_path.write_text(marp_markdown, encoding="utf-8")
        slides = self._build_slide_models(raw_slides, title, slide_visuals)
        presentation_html = self._inline_slide_assets(self._build_presentation_html(title, slides, user_request), folder)
        html_path.write_text(presentation_html, encoding="utf-8")
        pdf_provider = self._build_presentation_pdf(markdown_path, html_path, pdf_path, slides)

        files: list[dict[str, Any]] = []
        if html_path.exists():
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
        files.append(
            {
                "name": markdown_name,
                "path": str(markdown_path).replace("\\", "/"),
                "mime_type": "text/markdown",
                "download_url": f"/api/artefatos/{job_id}/{markdown_name}",
            }
        )

        summary = "Apresentacao profissional gerada em HTML e PDF."
        if slide_visuals:
            summary = "Apresentacao profissional gerada em HTML e PDF com composicao visual e imagens IA."
        else:
            summary = "Apresentacao profissional gerada em HTML e PDF com layout visual personalizado."
        raw = (
            f"Apresentacao gerada com sucesso: `{html_name}`, `{pdf_name}` e `{markdown_name}`."
            " Abra o HTML para revisar o deck visual e use o PDF para entrega final."
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
            "preview_url": f"/api/artefatos/{job_id}/{html_name}" if html_path.exists() else "",
            "slide_deck": True,
            "tools_used": ["presentation-html", "playwright", "marp-cli", *(["pollinations-image"] if slide_visuals else [])],
        }

    def _source_text(self, user_request: str, draft: dict[str, Any]) -> str:
        for key in ("raw", "summary"):
            value = str(draft.get(key) or "").strip()
            cleaned = self._normalize_document_text(self._strip_system_notices(value), user_request)
            if self._looks_like_document_content(cleaned):
                return cleaned
        return self._fallback_document_text(user_request)

    def _is_slide_request(self, user_request: str, text: str = "") -> bool:
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
        cleaned = self._normalize_slide_source(source_text).strip()
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
        normalized = self._normalize_slide_source(text)
        chunks = [self._sanitize_slide_chunk(chunk) for chunk in re.split(r"(?m)^\s*---+\s*$", normalized)]
        return [chunk for chunk in chunks if chunk]

    def _looks_like_marp_deck(self, text: str) -> bool:
        if "marp: true" in text.lower():
            return True
        return text.count("\n---") >= 1 and ("# " in text or "## " in text)

    def _split_into_slides(self, text: str) -> list[str]:
        normalized = self._normalize_slide_source(text)
        if "\n---" in normalized:
            parts = [self._sanitize_slide_chunk(chunk) for chunk in re.split(r"(?m)^\s*---+\s*$", normalized) if chunk.strip()]
            if parts:
                return parts
        sections = []
        current: list[str] = []
        for line in normalized.splitlines():
            stripped = line.strip()
            if re.match(r"^#{1,2}\s+", stripped) and current:
                sections.append(self._sanitize_slide_chunk("\n".join(current)))
                current = [stripped]
            else:
                current.append(stripped)
        if current:
            sections.append(self._sanitize_slide_chunk("\n".join(current)))
        sections = [section for section in sections if section]
        if len(sections) <= 1:
            return self._synthesize_slide_deck(normalized)
        normalized = [self._normalize_slide(section) for section in sections]
        if normalized:
            normalized[0] = self._upgrade_cover_slide(normalized[0])
        return normalized

    def _normalize_slide_source(self, text: str) -> str:
        cleaned = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not cleaned:
            return ""
        cleaned = re.sub(r"^```(?:markdown|md|text|html)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        lines = cleaned.splitlines()
        if lines and lines[0].strip() == "---":
            for idx in range(1, min(len(lines), 24)):
                if lines[idx].strip() == "---":
                    frontmatter_body = lines[1:idx]
                    if any(":" in item for item in frontmatter_body):
                        lines = lines[idx + 1 :]
                    break
        cleaned = "\n".join(lines)
        cleaned = re.sub(r"(?m)^\s*>\s*", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    def _sanitize_slide_chunk(self, chunk: str) -> str:
        lines: list[str] = []
        for raw_line in chunk.splitlines():
            cleaned = self._clean_slide_line(raw_line)
            if cleaned:
                lines.append(cleaned)
        return "\n".join(lines).strip()

    def _clean_slide_line(self, raw_line: str) -> str:
        stripped = raw_line.strip()
        if not stripped:
            return ""
        if stripped.startswith("<!--") or stripped.startswith("```"):
            return ""
        if re.fullmatch(r"-{3,}", stripped):
            return ""
        if re.match(r"^(marp|theme|paginate|size|headingDivider|footer)\s*:", stripped, flags=re.I):
            return ""
        if stripped.lower() == "style:" or re.match(r"^[a-z-]+\s*\{$", stripped.lower()):
            return ""
        if stripped.startswith("![") and "](" in stripped:
            return ""
        bullet_prefix = ""
        if re.match(r"^[-*]\s+", stripped):
            bullet_prefix = "- "
            stripped = re.sub(r"^[-*]\s+", "", stripped)
        elif re.match(r"^\d+[.)]\s+", stripped):
            bullet_prefix = "- "
            stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        stripped = re.sub(r"^#{1,6}\s*", "", stripped)
        stripped = re.sub(r"^>\s*", "", stripped)
        stripped = self._clean_inline_markdown(stripped)
        if not stripped or stripped in {"---", "--"}:
            return ""
        if re.fullmatch(r"[_*`~#\\\-.|: ]+", stripped):
            return ""
        return f"{bullet_prefix}{stripped}" if bullet_prefix else stripped

    def _synthesize_slide_deck(self, text: str) -> list[str]:
        title = self._slide_title_from_text(text)
        sentences = [chunk.strip(" -") for chunk in re.split(r"(?<=[\.\!\?])\s+", " ".join(text.split())) if chunk.strip()]
        bullets = [self._clean_inline_markdown(chunk) for chunk in sentences if chunk.strip()][:12]
        if not bullets:
            bullets = ["Contexto principal", "Pontos-chave", "Recomendacao final"]
        grouped = [bullets[index : index + 3] for index in range(0, len(bullets), 3)]
        slides = [self._upgrade_cover_slide(f"# {title}\n\n## {bullets[0]}")]
        for index, group in enumerate(grouped[:3], start=1):
            heading = "## Destaques" if index == 1 else f"## Topico {index}"
            slides.append("\n".join([heading, "", *[f"- {item}" for item in group]]))
        slides.append(self._render_closing_slide(f"## Fechamento\n\n- {bullets[-1]}", title))
        return [self._sanitize_slide_chunk(slide) for slide in slides if slide.strip()]

    def _needs_slide_fallback(self, source_text: str, user_request: str) -> bool:
        compact = " ".join(source_text.split()).lower()
        if len(compact) < 260:
            return True
        weak_markers = [
            "documento executivo",
            "contexto principal",
            "pontos-chave",
            "recomendacao final",
            "recomendação final",
            "panorama claro, visual e pronto",
            "estrutura base de documento",
            "este documento apresenta",
            "contexto geral",
            "em um documento final",
            "este pdf e docx",
        ]
        if any(marker in compact for marker in weak_markers):
            return True
        topic = self._extract_topic(user_request).lower()
        topic_terms = [term for term in re.split(r"\W+", topic) if len(term) >= 5][:4]
        if topic_terms and not any(term in compact for term in topic_terms):
            return True
        return False

    def _fallback_slide_deck_text(self, user_request: str, title: str) -> str:
        topic = self._clean_inline_markdown(self._extract_topic(user_request) or title)
        lowered = user_request.lower()
        wants_iso = any(token in lowered for token in ["iso", "norma", "normas", "certificacao", "certificação"])
        wants_steps = any(token in lowered for token in ["roadmap", "passos", "plano", "implementacao", "implementação"])
        wants_images = any(token in lowered for token in ["imagem", "visual", "ilustrado", "premium", "gamma", "canva"])
        standards = [
            "ISO 55001 - sistema de gestao de ativos e governanca do ciclo de vida",
            "ISO/IEC 19770 - gestao de ativos de TI, inventario, licencas e conformidade",
            "ISO/IEC 27001 - seguranca da informacao aplicada a ativos criticos",
            "ISO/IEC 20000-1 - gestao de servicos de TI conectada a catalogo e suporte",
        ]
        standards_slide = "\n".join(f"- {item}" for item in standards)
        general_references = "\n".join(
            [
                "- Boas praticas de gestao de ativos orientadas a ciclo de vida",
                "- Controles de seguranca e conformidade para ativos criticos",
                "- Governanca de servicos e processos para TI operacional",
                "- Metricas executivas para custo, risco, disponibilidade e uso",
            ]
        )
        references_slide = standards_slide if wants_iso else general_references
        visual_note = "visual executivo com imagens e diagramas" if wants_images else "visual executivo com hierarquia clara"
        roadmap_title = "Roadmap de implementacao" if wants_steps else "Modelo operacional"
        return f"""# {topic}
## {visual_note.capitalize()} para decisao e apresentacao

---

## O problema que precisa ser resolvido
- Inventarios incompletos reduzem visibilidade sobre hardware, software e contratos
- Custos crescem quando licencas, garantias e ativos ociosos nao sao reconciliados
- Riscos de seguranca aumentam quando ativos criticos nao tem dono, status e ciclo de vida claros

---

## Objetivos do programa
- Visibilidade total - consolidar inventario, responsaveis, criticidade e localizacao
- Controle financeiro - reduzir desperdicio, duplicidade e renovacoes sem uso
- Governanca - criar regras para aquisicao, uso, manutencao e descarte
- Seguranca - conectar ativos a vulnerabilidades, acessos e continuidade

---

## Normas e referencias aplicaveis
{references_slide}

---

## Arquitetura de gestao
- Base unica - CMDB, inventario, contratos e telemetria integrados
- Dono do ativo - responsabilidade clara por criticidade, custo e risco
- Eventos de ciclo de vida - entrada, movimentacao, manutencao, renovacao e descarte
- Indicadores - custo total, cobertura, conformidade, risco e disponibilidade

---

## {roadmap_title}
- Diagnosticar - mapear fontes, lacunas, ativos criticos e contratos relevantes
- Integrar - unificar inventario, descoberta automatica e dados financeiros
- Governar - definir papeis, politicas, aprovacao e trilhas de auditoria
- Otimizar - revisar licencas, riscos, ativos ociosos e oportunidades de economia

---

## Indicadores para acompanhar
- Cobertura de inventario - percentual de ativos conhecidos e classificados
- Custo evitado - economia por reuso, renegociacao e remocao de desperdicio
- Risco reduzido - ativos criticos com patch, dono e controle de acesso
- Conformidade - aderencia a politicas internas, normas e evidencias de auditoria

---

## Fechamento
- Comece pelos ativos criticos e fontes mais confiaveis
- Transforme inventario em decisao financeira, operacional e de seguranca
- Mantenha revisoes recorrentes para que a base nao volte a ficar obsoleta
"""

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
        if not self.settings.pollinations_api_key or not slides or not self._wants_ai_slide_images(user_request):
            return {}
        selected_indexes = self._select_visual_slides(slides, user_request)
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
                visuals[slide_index] = f"assets/{output_path.name}"
            except Exception:
                continue
        return visuals

    def _select_visual_slides(self, slides: list[str], user_request: str = "") -> list[int]:
        choices: list[int] = []
        lowered_request = user_request.lower()
        if slides:
            choices.append(0)
        for idx, slide in enumerate(slides[1:-1], start=1):
            lower = slide.lower()
            if any(token in lower for token in ["impacto", "cenario", "cenario", "mercado", "processo", "cronograma", "arquitetura", "roadmap", "estrategia", "estratégia"]):
                choices.append(idx)
            if len(choices) >= 4:
                break
        if len(choices) < 2 and len(slides) > 2:
            choices.append(1)
        if any(token in lowered_request for token in ["imagem", "imagens", "visual", "ilustrado", "com fotos", "com foto"]) and len(slides) > 3:
            choices.append(min(2, len(slides) - 2))
        deduped: list[int] = []
        for item in choices:
            if item not in deduped:
                deduped.append(item)
        return deduped[:4]

    def _wants_ai_slide_images(self, user_request: str) -> bool:
        lowered = user_request.lower()
        return any(
            token in lowered
            for token in [
                "com imagem",
                "com imagens",
                "com foto",
                "com fotos",
                "ilustrado",
                "ilustrações",
                "ilustracoes",
                "visual com imagem",
                "use imagens",
                "usar imagens",
            ]
        )

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
        style_hint = self._image_style_hint(user_request)
        is_cover = slide_index == 0
        if is_cover:
            return (
                f"Premium presentation cover image for '{deck_title}'. "
                f"Theme: {heading}. "
                f"Editorial, cinematic, polished corporate storytelling, high-end consulting deck aesthetic, "
                f"{style_hint} clean composition with negative space for title text, subtle depth, modern lighting, no text, no watermark. "
                f"Context: {user_request[:260]}"
            )
        return (
            f"Presentation visual for slide {slide_index + 1} of {total_slides} about '{deck_title}'. "
            f"Slide topic: {heading}. "
            f"Key points: {narrative or user_request[:220]}. "
            "Professional editorial illustration or photoreal concept for a boardroom-grade presentation, "
            f"{style_hint} clean composition, sophisticated color palette, suitable for split-slide layout, no text, no watermark."
        )

    def _image_style_hint(self, user_request: str) -> str:
        lowered = user_request.lower()
        if any(token in lowered for token in ["realista", "fotorealista", "foto"]):
            return "Photoreal, realistic, premium business photography,"
        if any(token in lowered for token in ["3d", "futurista", "neon"]):
            return "Futuristic 3D render language,"
        if any(token in lowered for token in ["minimal", "minimalista", "clean"]):
            return "Minimal editorial visual language,"
        if any(token in lowered for token in ["luxo", "premium", "executivo"]):
            return "Luxury executive editorial visual language,"
        return "Professional presentation visual language,"

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

    def _build_slide_models(self, raw_slides: list[str], deck_title: str, slide_visuals: dict[int, str]) -> list[Slide]:
        slides: list[Slide] = []
        total = len(raw_slides)
        for index, raw_slide in enumerate(raw_slides):
            layout = self._infer_slide_class(raw_slide, index, total)
            lines = [line for line in (self._clean_slide_line(item) for item in raw_slide.splitlines()) if line]
            title_line = next((line for line in lines if not line.startswith("- ")), lines[0] if lines else deck_title)
            title = self._humanize_slide_title(title_line, index, deck_title)
            bullets = [self._clean_inline_markdown(re.sub(r"^-\s+", "", line).strip()) for line in lines if line.startswith("- ")]
            extras = [line for line in lines if line != title_line and not line.startswith("- ")]
            rows = self._extract_slide_table(raw_slide)
            kicker = self._slide_kicker(deck_title, layout, index)
            body = [self._clean_inline_markdown(item) for item in extras if not self._looks_like_table_line(item)]
            body = [item for item in body if item and item != title and item.lower() != kicker.lower()]
            bullets = [item for item in bullets if item and item != title][:4]
            if layout == "lead" and not body and bullets:
                body = [bullets[0]]
                bullets = bullets[1:]
            slides.append(
                Slide(
                    title=title,
                    kicker=kicker,
                    body=body[:2],
                    bullets=bullets,
                    rows=rows,
                    layout=layout,
                    visual=slide_visuals.get(index) or self._slide_visual_fallback_url(deck_title, raw_slide, index, total),
                )
            )
        return slides

    def _humanize_slide_title(self, raw_title: str, index: int, deck_title: str) -> str:
        cleaned = self._clean_inline_markdown(re.sub(r"^#{1,3}\s*", "", raw_title or "")).strip()
        cleaned = re.sub(r"^(slide|pagina|página|page)\s*\d+\s*[-–:]\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"^\d+\s*[-–.:]\s*", "", cleaned)
        return cleaned or (deck_title if index == 0 else f"Topico {index + 1}")

    def _extract_slide_table(self, raw_slide: str) -> list[list[str]] | None:
        lines = [line.strip() for line in raw_slide.splitlines() if line.strip()]
        table_lines = [line for line in lines if self._looks_like_table_line(line)]
        rows = self._parse_markdown_table(table_lines) if table_lines else []
        return rows or None

    def _slide_kicker(self, deck_title: str, layout: str, index: int) -> str:
        mapping = {
            "lead": deck_title,
            "agenda": "Visao do percurso",
            "metrics": "Sinais que importam",
            "timeline": "Leitura em etapas",
            "compare": "Comparacao executiva",
            "highlights": "Prioridades executivas",
            "closing": "Mensagem final",
            "section": "Ponto de analise",
        }
        return mapping.get(layout, f"Slide {index + 1}")

    def _slide_visual_fallback_url(self, deck_title: str, raw_slide: str, index: int, total: int) -> str | None:
        if index not in {0, 2, 4}:
            return None
        prompt = self._slide_image_prompt(deck_title, deck_title, raw_slide, index, total)
        prompt = f"{prompt} dark mode, neon blue accents, professional, minimalist, editorial presentation"
        encoded = quote(prompt, safe="")
        return f"https://pollinations.ai/p/{encoded}?width=1280&height=720&nologo=true"

    def _build_presentation_html(self, deck_title: str, slides: list[Slide], user_request: str = "") -> str:
        theme_css = self._deck_theme_css(user_request, deck_title)
        slide_markup = "\n".join(self._render_slide_html(slide, index, len(slides)) for index, slide in enumerate(slides))
        return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{self._escape_html(deck_title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #0f1d2e;
      --panel: rgba(255,255,255,0.88);
      --panel-soft: rgba(255,255,255,0.7);
      --line: rgba(17, 35, 58, 0.09);
      --ink: #10233d;
      --muted: #61748f;
      --accent: #1f5ea8;
      --accent-2: #27a6b8;
      --shadow: 0 28px 60px rgba(14, 31, 53, 0.12);
      --radius: 28px;
      --slide-w: 1280px;
      --slide-h: 720px;
    }}
    {theme_css}
    @page {{
      size: 13.333in 7.5in;
      margin: 0;
    }}
    * {{
      box-sizing: border-box;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      background: #0b1320;
      color: var(--ink);
      font-family: Inter, Aptos, "Segoe UI", Arial, sans-serif;
    }}
    body {{
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
    }}
    .deck {{
      width: min(100%, var(--slide-w));
      display: grid;
      gap: 26px;
    }}
    body.pdf-export {{
      width: 1280px;
      min-height: auto;
      display: block;
      padding: 0;
      background: #fff;
    }}
    body.pdf-export .deck {{
      width: 1280px;
      gap: 0;
    }}
    .slide-container {{
      width: var(--slide-w);
      max-width: 100%;
      break-inside: avoid;
      page-break-after: always;
    }}
    .slide-container:last-child {{
      page-break-after: auto;
    }}
    body.pdf-export .slide {{
      width: 1280px;
      height: 720px;
      aspect-ratio: auto;
      border-radius: 0;
      box-shadow: none;
      page-break-after: always;
      break-after: page;
    }}
    body.pdf-export .slide:last-child {{
      page-break-after: auto;
      break-after: auto;
    }}
    body.pdf-export .deck-nav {{
      display: none !important;
    }}
    .slide {{
      position: relative;
      width: var(--slide-w);
      max-width: 100%;
      min-height: var(--slide-h);
      height: var(--slide-h);
      overflow: hidden;
      border-radius: 30px;
      background:
        radial-gradient(circle at top left, rgba(31, 94, 168, 0.22), transparent 34%),
        linear-gradient(180deg, #f7fbff 0%, #eef4fa 100%);
      box-shadow: 0 42px 90px rgba(8, 19, 36, 0.28);
      display: grid;
      grid-template-columns: minmax(0, 1.08fr) minmax(320px, .92fr);
      isolation: isolate;
    }}
    .slide-content {{
      padding: 48px 56px 48px;
      padding: 48px 56px 48px;
      display: flex;
      flex-direction: column;
      min-width: 0;
      gap: 16px;
      z-index: 2;
    }}
    .slide-meta {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      color: var(--muted);
      font-size: 15px;
      font-weight: 600;
      letter-spacing: .01em;
    }}
    .slide-kicker {{
      text-transform: uppercase;
      letter-spacing: .12em;
      font-size: 11px;
      color: var(--accent);
      font-weight: 800;
    }}
    .slide-page {{
      font-size: 13px;
    }}
    .slide-title {{
      margin: 0;
      font-size: 50px;
      line-height: 1.02;
      letter-spacing: 0;
      color: #0c2848;
      max-width: 11ch;
      text-wrap: balance;
    }}
    .slide-subtitle {{
      margin: 0;
      font-size: 20px;
      line-height: 1.45;
      color: #41556f;
      max-width: 27ch;
      text-wrap: balance;
    }}
    .slide-visual {{
      position: relative;
      min-width: 0;
      background: linear-gradient(135deg, #123255 0%, #1a5d85 100%);
    }}
    .slide-visual img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .slide-visual::after {{
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg, rgba(247,251,255,0.02) 0%, rgba(12, 28, 49, 0.18) 100%);
    }}
    .visual-map {{
      position: absolute;
      inset: 54px;
      display: grid;
      place-items: center;
      color: rgba(255,255,255,0.92);
    }}
    .visual-orbit {{
      position: relative;
      width: 360px;
      height: 360px;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.22);
      background:
        radial-gradient(circle at center, rgba(255,255,255,0.20), transparent 28%),
        radial-gradient(circle at 30% 20%, rgba(94,234,212,0.26), transparent 20%);
    }}
    .visual-node {{
      position: absolute;
      width: 118px;
      min-height: 54px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 10px 14px;
      background: rgba(255,255,255,0.14);
      border: 1px solid rgba(255,255,255,0.20);
      font-size: 13px;
      font-weight: 800;
      letter-spacing: .04em;
      text-transform: uppercase;
      backdrop-filter: blur(14px);
    }}
    .visual-node:nth-child(1) {{
      top: -8px;
      left: 50%;
      transform: translateX(-50%);
    }}
    .visual-node:nth-child(2) {{
      right: -38px;
      top: 48%;
      transform: translateY(-50%);
    }}
    .visual-node:nth-child(3) {{
      bottom: -8px;
      left: 50%;
      transform: translateX(-50%);
    }}
    .visual-node:nth-child(4) {{
      left: -38px;
      top: 48%;
      transform: translateY(-50%);
    }}
    .visual-core {{
      position: absolute;
      inset: 122px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 20px;
      background: rgba(255,255,255,0.92);
      color: #0c2848;
      font-size: 20px;
      font-weight: 900;
      box-shadow: 0 22px 54px rgba(4, 17, 34, 0.20);
    }}
    .content-stack {{
      display: flex;
      flex-direction: column;
      gap: 14px;
      min-width: 0;
    }}
    .bullet-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 12px;
    }}
    .bullet-list li {{
      display: grid;
      grid-template-columns: 14px minmax(0, 1fr);
      gap: 14px;
      align-items: start;
      font-size: 21px;
      line-height: 1.36;
      color: #132942;
    }}
    .bullet-list li::before {{
      content: "";
      width: 14px;
      height: 14px;
      margin-top: 11px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 0 0 8px rgba(31, 94, 168, 0.08);
    }}
    .agenda-grid, .metrics-grid, .highlights-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .agenda-card, .metric-card, .highlight-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px 22px;
      box-shadow: var(--shadow);
      min-height: 0;
    }}
    .highlight-card {{
      background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(244,249,255,0.94));
      min-height: 150px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 12px;
    }}
    .agenda-card strong, .metric-label {{
      display: block;
      font-size: 15px;
      text-transform: uppercase;
      letter-spacing: .08em;
      color: var(--accent);
      margin-bottom: 12px;
    }}
    .agenda-card span {{
      display: block;
      font-size: 24px;
      line-height: 1.22;
      color: #10233d;
      font-weight: 700;
      text-wrap: balance;
    }}
    .metric-value {{
      display: block;
      font-size: 34px;
      line-height: 1;
      letter-spacing: -.04em;
      color: #0c2848;
      margin: 8px 0 10px;
      font-weight: 800;
    }}
    .metric-text {{
      font-size: 18px;
      line-height: 1.45;
      color: #4b5e78;
    }}
    .highlight-label {{
      display: inline-flex;
      width: fit-content;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(31, 94, 168, 0.08);
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .highlight-text {{
      font-size: 22px;
      line-height: 1.24;
      color: #10233d;
      font-weight: 700;
      letter-spacing: -.03em;
    }}
    .highlight-detail {{
      font-size: 15px;
      line-height: 1.45;
      color: #51657f;
    }}
    .timeline {{
      display: grid;
      gap: 16px;
    }}
    .timeline-step {{
      position: relative;
      padding: 0 0 0 68px;
      min-height: 52px;
      display: flex;
      align-items: center;
      font-size: 21px;
      line-height: 1.34;
      color: #10233d;
    }}
    .timeline-step::before {{
      content: attr(data-step);
      position: absolute;
      left: 0;
      top: 0;
      width: 46px;
      height: 46px;
      border-radius: 999px;
      background: linear-gradient(135deg, #123d72, #1da6b7);
      color: #fff;
      display: grid;
      place-items: center;
      font-weight: 800;
      box-shadow: 0 12px 24px rgba(16, 42, 72, 0.18);
    }}
    .timeline-step::after {{
      content: "";
      position: absolute;
      left: 22px;
      top: 46px;
      width: 2px;
      bottom: -18px;
      background: rgba(18, 61, 114, 0.18);
    }}
    .timeline-step:last-child::after {{
      display: none;
    }}
    .compare-table {{
      width: 100%;
      border-collapse: collapse;
      overflow: hidden;
      border-radius: 24px;
      background: rgba(255,255,255,0.92);
      box-shadow: var(--shadow);
    }}
    .compare-table th, .compare-table td {{
      padding: 16px 18px;
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid rgba(16, 35, 61, 0.08);
      font-size: 18px;
      line-height: 1.4;
    }}
    .compare-table th {{
      background: #10345e;
      color: #fff;
      font-size: 15px;
      letter-spacing: .04em;
      text-transform: uppercase;
    }}
    .compare-table tr:last-child td {{
      border-bottom: 0;
    }}
    .lead {{
      grid-template-columns: minmax(0, 1.02fr) minmax(320px, .98fr);
      background:
        radial-gradient(circle at top left, rgba(36, 172, 196, 0.24), transparent 26%),
        linear-gradient(135deg, #0e1f33 0%, #163658 52%, #215b83 100%);
    }}
    .lead .slide-content {{
      padding-top: 58px;
      padding-bottom: 58px;
      justify-content: center;
    }}
    .lead .slide-kicker,
    .lead .slide-page,
    .lead .slide-subtitle {{
      color: rgba(255,255,255,0.8);
    }}
    .lead .slide-page {{
      color: rgba(255,255,255,0.9);
    }}
    .lead .slide-title {{
      color: #fff;
      font-size: 58px;
      max-width: 8.8ch;
    }}
    .lead .slide-subtitle {{
      font-size: 22px;
      max-width: 22ch;
    }}
    .lead .bullet-list li {{
      color: rgba(255,255,255,0.78);
    }}
    .lead .bullet-list li::before {{
      background: linear-gradient(135deg, #5eead4, #38bdf8);
      box-shadow: 0 0 0 8px rgba(94, 234, 212, 0.12);
    }}
    .lead .lead-chip {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 12px 16px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      color: rgba(255,255,255,0.92);
      font-size: 15px;
      width: fit-content;
      backdrop-filter: blur(12px);
    }}
    .lead .bullet-list li {{
      color: rgba(255,255,255,0.95);
      font-size: 18px;
      line-height: 1.3;
    }}
    .lead .bullet-list li::before {{
      background: linear-gradient(135deg, #68ecff, #d8faff);
      box-shadow: 0 0 0 8px rgba(255,255,255,0.08);
    }}
    .closing {{
      background:
        radial-gradient(circle at top right, rgba(39, 166, 184, 0.18), transparent 28%),
        linear-gradient(135deg, #10243d 0%, #1b4977 100%);
    }}
    .closing .slide-title,
    .closing .slide-subtitle,
    .closing .slide-kicker,
    .closing .slide-page,
    .closing .bullet-list li {{
      color: #fff;
    }}
    .closing .bullet-list li::before {{
      background: linear-gradient(135deg, #4fe0f2, #ffffff);
      box-shadow: 0 0 0 8px rgba(255,255,255,0.08);
    }}
    .content {{
      grid-template-columns: minmax(0, 1.18fr) minmax(320px, .82fr);
    }}
    .content .slide-title {{
      max-width: 14ch;
    }}
    .content.no-visual,
    .closing.no-visual,
    .lead.no-visual {{
      grid-template-columns: 1fr;
    }}
    .content.no-visual .slide-content,
    .closing.no-visual .slide-content {{
      max-width: 940px;
    }}
    .slide.no-visual .slide-visual {{
      display: none;
    }}
    .slide.copy-heavy .bullet-list li {{
      font-size: 18px;
    }}
    .slide.copy-heavy .slide-title {{
      font-size: 42px;
    }}
    .slide.long-title .slide-title {{
      font-size: 42px;
      max-width: 18ch;
    }}
    .lead.long-title .slide-title {{
      font-size: 54px;
      max-width: 13ch;
    }}
    .deck-nav {{
      position: fixed;
      right: 24px;
      bottom: 22px;
      z-index: 20;
      display: flex;
      gap: 10px;
      background: rgba(9, 18, 32, 0.84);
      color: #fff;
      padding: 10px 14px;
      border-radius: 999px;
      box-shadow: 0 16px 30px rgba(0,0,0,0.24);
      backdrop-filter: blur(14px);
      font-size: 14px;
    }}
    .deck-nav button {{
      border: 0;
      background: rgba(255,255,255,0.08);
      color: inherit;
      padding: 10px 12px;
      border-radius: 999px;
      cursor: pointer;
      font: inherit;
    }}
    .deck-nav button:hover {{
      background: rgba(255,255,255,0.16);
    }}
    @media screen and (max-width: 1100px) {{
      body {{
        padding: 10px;
      }}
      .deck {{
        gap: 12px;
      }}
      .slide-container,
      .slide {{
        width: 100%;
        min-height: auto;
        height: auto;
        border-radius: 18px;
      }}
      .slide-content {{
        padding: 26px;
      }}
      .slide-title {{
        font-size: 34px;
      }}
      .slide-subtitle,
      .bullet-list li,
      .timeline-step,
      .agenda-card span {{
        font-size: 18px;
      }}
    }}
    @media print {{
      @page {{
        size: 13.333in 7.5in;
        margin: 0;
      }}
      * {{
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
      }}
      html,
      body {{
        background: #fff;
        padding: 0;
        margin: 0;
      }}
      .deck {{
        width: 100%;
        gap: 0;
      }}
      .slide-container {{
        width: 1280px !important;
        height: 720px !important;
        overflow: hidden;
        page-break-after: always;
        break-after: page;
        break-inside: avoid;
      }}
      .slide {{
        width: 1280px !important;
        height: 720px !important;
        margin: 0;
        border-radius: 0;
        box-shadow: none;
      }}
      .deck-nav {{
        display: none !important;
      }}
    }}
  </style>
</head>
<body>
  <main class="deck">
    {slide_markup}
  </main>
  <div class="deck-nav" aria-hidden="true">
    <button onclick="stepSlide(-1)">Anterior</button>
    <button onclick="stepSlide(1)">Próximo</button>
  </div>
  <script>
    const slides = Array.from(document.querySelectorAll('.slide'));
    let activeSlide = 0;
    function stepSlide(direction) {{
      activeSlide = Math.max(0, Math.min(slides.length - 1, activeSlide + direction));
      slides[activeSlide].scrollIntoView({{ behavior: 'smooth', block: 'center' }});
    }}
    document.addEventListener('keydown', (event) => {{
      if (event.key === 'ArrowRight' || event.key === 'PageDown') stepSlide(1);
      if (event.key === 'ArrowLeft' || event.key === 'PageUp') stepSlide(-1);
    }});
  </script>
</body>
</html>"""

    def _render_slide_html(self, slide: Slide, index: int, total: int) -> str:
        classes = [slide.layout if slide.layout in {"lead", "agenda", "metrics", "timeline", "compare", "closing", "highlights"} else "content"]
        if not slide.visual:
            if slide.layout in {"agenda", "metrics", "timeline", "compare", "highlights"}:
                classes.append("no-visual")
            else:
                classes.append("generated-visual")
        if len(slide.title) > 44:
            classes.append("long-title")
        if len(slide.bullets or []) >= 4:
            classes.append("copy-heavy")
        content = self._render_slide_content(slide)
        visual_html = self._render_visual_html(slide)
        title = self._escape_html(slide.title)
        kicker = self._escape_html(slide.kicker)
        subtitle = self._escape_html(slide.body[0]) if slide.layout == "lead" and slide.body else ""
        lead_chip = '<div class="lead-chip">Deck personalizado para leitura executiva</div>' if slide.layout == "lead" else ""
        return (
            f'<article class="slide-container"><section class="slide {" ".join(classes)}" id="slide-{index + 1}">'
            '<div class="slide-content">'
            f'<div class="slide-meta"><span class="slide-kicker" contenteditable="true">{kicker}</span><span class="slide-page">{index + 1} / {total}</span></div>'
            f'<h1 class="slide-title" contenteditable="true">{title}</h1>'
            + (f'<p class="slide-subtitle" contenteditable="true">{subtitle}</p>' if subtitle else "")
            + lead_chip
            + f'<div class="content-stack">{content}</div>'
            + '</div>'
            + visual_html
            + '</section></article>'
        )

    def _render_visual_html(self, slide: Slide) -> str:
        if not slide.visual:
            return self._render_generated_visual_html(slide)
        if slide.visual.startswith(("http://", "https://", "data:")):
            visual = self._escape_html(slide.visual)
        else:
            visual = quote(slide.visual.replace("\\", "/"), safe="/:.-_")
        return f'<aside class="slide-visual"><img src="{visual}" alt="{self._escape_html(slide.title)}" /></aside>'

    def _render_generated_visual_html(self, slide: Slide) -> str:
        labels = [self._split_card_item(item)[0] for item in (slide.bullets or [])[:4]]
        if len(labels) < 4:
            labels.extend(["Inventario", "Risco", "Custo", "Governanca"][len(labels) : 4])
        nodes = "".join(f'<span class="visual-node">{self._escape_html(label[:26])}</span>' for label in labels[:4])
        core = self._escape_html((slide.title or "Estrategia")[:32])
        return (
            '<aside class="slide-visual">'
            '<div class="visual-map">'
            f'<div class="visual-orbit">{nodes}<strong class="visual-core">{core}</strong></div>'
            '</div>'
            '</aside>'
        )

    def _render_slide_content(self, slide: Slide) -> str:
        if slide.layout == "agenda":
            items = slide.bullets or slide.body or []
            cards = []
            for item in items[:4]:
                label, detail = self._split_agenda_item(item)
                cards.append(
                    '<article class="agenda-card">'
                    f'<strong contenteditable="true">{self._escape_html(label)}</strong>'
                    f'<span contenteditable="true">{self._escape_html(detail)}</span>'
                    '</article>'
                )
            return f'<div class="agenda-grid">{"".join(cards)}</div>'
        if slide.layout == "metrics":
            cards = []
            for bullet in (slide.bullets or [])[:4]:
                label, value, detail = self._parse_metric_bullet(f"- {bullet}")
                cards.append(
                    '<article class="metric-card">'
                    f'<span class="metric-label" contenteditable="true">{self._escape_html(label)}</span>'
                    f'<span class="metric-value" contenteditable="true">{self._escape_html(value)}</span>'
                    f'<span class="metric-text" contenteditable="true">{self._escape_html(detail)}</span>'
                    '</article>'
                )
            return f'<div class="metrics-grid">{"".join(cards)}</div>'
        if slide.layout == "timeline":
            steps = []
            for idx, bullet in enumerate((slide.bullets or [])[:5], start=1):
                steps.append(f'<div class="timeline-step" data-step="{idx}" contenteditable="true">{self._escape_html(bullet)}</div>')
            return f'<div class="timeline">{"".join(steps)}</div>'
        if slide.layout == "highlights":
            cards = []
            for bullet in (slide.bullets or [])[:4]:
                label, detail = self._split_card_item(bullet)
                cards.append(
                    '<article class="highlight-card">'
                    f'<span class="highlight-label">{self._escape_html(label)}</span>'
                    f'<div class="highlight-text">{self._escape_html(detail)}</div>'
                    f'<div class="highlight-detail">{self._escape_html(self._short_support_copy(detail))}</div>'
                    '</article>'
                )
            return f'<div class="highlights-grid">{"".join(cards)}</div>'
        if slide.layout == "compare" and slide.rows:
            return self._render_custom_table(slide.rows)
        items = slide.bullets or slide.body or []
        if items:
            bullets = "".join(f'<li contenteditable="true"><span>{self._format_inline_html(item)}</span></li>' for item in items)
            return f'<ul class="bullet-list">{bullets}</ul>'
        return f'<p class="slide-subtitle" contenteditable="true">{self._escape_html(slide.kicker)}</p>'

    def _render_custom_table(self, rows: list[list[str]]) -> str:
        header = "".join(f'<th contenteditable="true">{self._escape_html(cell)}</th>' for cell in rows[0])
        body = "".join(
            "<tr>" + "".join(f'<td contenteditable="true">{self._format_inline_html(cell)}</td>' for cell in row) + "</tr>"
            for row in rows[1:]
        )
        return f'<table class="compare-table"><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>'

    def _split_agenda_item(self, item: str) -> tuple[str, str]:
        cleaned = self._clean_inline_markdown(item)
        if " - " in cleaned:
            left, right = cleaned.split(" - ", 1)
            return left.strip(), right.strip()
        words = cleaned.split()
        if len(words) <= 2:
            return cleaned, "Ponto central do percurso"
        return words[0], " ".join(words[1:])

    def _split_card_item(self, item: str) -> tuple[str, str]:
        cleaned = self._clean_inline_markdown(item)
        for token in [" - ", ": ", " — ", " -- "]:
            if token in cleaned:
                left, right = cleaned.split(token, 1)
                return left.strip()[:48], right.strip()
        words = cleaned.split()
        if len(words) <= 3:
            return cleaned, "Ponto principal do slide"
        return " ".join(words[:2]), " ".join(words[2:])

    def _deck_theme_css(self, user_request: str, deck_title: str) -> str:
        lowered = f"{user_request}\n{deck_title}".lower()
        themes = {
            "minimal": """
    :root {
      --bg: #edf2f7;
      --panel: rgba(255,255,255,0.96);
      --panel-soft: rgba(255,255,255,0.82);
      --line: rgba(15, 23, 42, 0.08);
      --ink: #0f172a;
      --muted: #5b6472;
      --accent: #1d4ed8;
      --accent-2: #22c55e;
      --shadow: 0 24px 52px rgba(15, 23, 42, 0.10);
    }
            """,
            "dark": """
    :root {
      --bg: #050816;
      --panel: rgba(10,18,34,0.74);
      --panel-soft: rgba(15,24,44,0.58);
      --line: rgba(148,163,184,0.12);
      --ink: #e5eefb;
      --muted: #a8bad5;
      --accent: #60a5fa;
      --accent-2: #22d3ee;
      --shadow: 0 28px 60px rgba(2, 6, 23, 0.38);
    }
    .slide {
      background:
        radial-gradient(circle at top left, rgba(96, 165, 250, 0.22), transparent 34%),
        linear-gradient(180deg, #08101f 0%, #0d1a31 100%);
    }
    .slide-title, .agenda-card span, .highlight-text, .timeline-step, .bullet-list li, .metric-value {
      color: #f8fbff;
    }
    .slide-subtitle, .metric-text, .highlight-detail {
      color: #c7d6ea;
    }
    .agenda-card, .metric-card, .highlight-card, .compare-table {
      background: rgba(8,16,31,0.66);
    }
            """,
            "executive": """
    :root {
      --bg: #0f1d2e;
      --panel: rgba(255,255,255,0.88);
      --panel-soft: rgba(255,255,255,0.7);
      --line: rgba(17, 35, 58, 0.09);
      --ink: #10233d;
      --muted: #61748f;
      --accent: #1f5ea8;
      --accent-2: #27a6b8;
      --shadow: 0 28px 60px rgba(14, 31, 53, 0.12);
    }
            """,
            "warm": """
    :root {
      --bg: #2d160e;
      --panel: rgba(255,250,244,0.90);
      --panel-soft: rgba(255,247,237,0.76);
      --line: rgba(120, 53, 15, 0.12);
      --ink: #40210f;
      --muted: #8a5b3d;
      --accent: #c2410c;
      --accent-2: #f59e0b;
      --shadow: 0 30px 60px rgba(82, 36, 10, 0.16);
    }
            """,
        }
        if any(token in lowered for token in ["dark", "escuro", "noturno", "futurista", "neon"]):
            return themes["dark"]
        if any(token in lowered for token in ["minimal", "minimalista", "clean", "limpo"]):
            return themes["minimal"]
        if any(token in lowered for token in ["quente", "laranja", "criativo", "editorial", "premium"]):
            return themes["warm"]
        return themes["executive"]

    def _inline_slide_assets(self, html: str, base_dir: Path) -> str:
        def replace_src(match: re.Match[str]) -> str:
            original = match.group(1)
            if original.startswith(("http://", "https://", "data:")):
                return match.group(0)
            asset_path = (base_dir / original).resolve()
            if not asset_path.exists() or not asset_path.is_file():
                return match.group(0)
            mime_type = "image/png"
            if asset_path.suffix.lower() in {".jpg", ".jpeg"}:
                mime_type = "image/jpeg"
            elif asset_path.suffix.lower() == ".webp":
                mime_type = "image/webp"
            elif asset_path.suffix.lower() == ".svg":
                mime_type = "image/svg+xml"
            data_url = f"data:{mime_type};base64,{base64.b64encode(asset_path.read_bytes()).decode('ascii')}"
            return match.group(0).replace(original, data_url)

        return re.sub(r'src="([^"]+)"', replace_src, html)

    def _prepare_slide_pdf_html(self, html: str) -> str:
        if "<body>" in html:
            return html.replace("<body>", '<body class="pdf-export">', 1)
        return html.replace("<body ", '<body class="pdf-export" ', 1)

    def _short_support_copy(self, detail: str) -> str:
        words = detail.split()
        if len(words) <= 12:
            return detail
        return " ".join(words[:12]).rstrip(" ,.;:") + "."

    def _format_inline_html(self, text: str) -> str:
        escaped = self._escape_html(text)
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    def _build_presentation_pdf(self, markdown_path: Path, html_path: Path, pdf_path: Path, slides: list[Slide]) -> str:
        html_text = self._prepare_slide_pdf_html(self._inline_slide_assets(html_path.read_text(encoding="utf-8"), html_path.parent))
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=1.5)
                page.set_content(html_text, wait_until="networkidle")
                page.emulate_media(media="print")
                page.pdf(
                    path=str(pdf_path),
                    width="1280px",
                    height="720px",
                    print_background=True,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )
                browser.close()
            return "playwright-html"
        except Exception:
            provider = self._render_slide_pdf(markdown_path, html_path, pdf_path, html_text)
            if pdf_path.exists():
                return provider
            self._build_slide_pdf_with_reportlab(pdf_path, slides)
            return "reportlab-slides"

    def _build_slide_pdf_with_reportlab(self, path: Path, slides: list[Slide]) -> None:
        width, height = 13.333 * inch, 7.5 * inch
        pdf = pdf_canvas.Canvas(str(path), pagesize=(width, height))
        for index, slide in enumerate(slides):
            self._draw_slide_page(pdf, slide, index, len(slides), width, height)
            pdf.showPage()
        pdf.save()

    def _draw_slide_page(self, pdf: Any, slide: Slide, index: int, total: int, width: float, height: float) -> None:
        dark = slide.layout in {"lead", "closing"}
        bg = colors.HexColor("#123657") if dark else colors.HexColor("#f3f8fd")
        accent = colors.HexColor("#27a6b8")
        ink = colors.white if dark else colors.HexColor("#0c2848")
        muted = colors.HexColor("#d7e8f7") if dark else colors.HexColor("#49637f")
        pdf.setFillColor(bg)
        pdf.rect(0, 0, width, height, fill=1, stroke=0)
        pdf.setFillColor(colors.HexColor("#1e5a84") if dark else colors.HexColor("#dfeaf4"))
        pdf.rect(width * 0.56, 0, width * 0.44, height, fill=1, stroke=0)
        pdf.setFillColor(accent)
        pdf.circle(width * 0.86, height * 0.25, 90, fill=1, stroke=0)
        pdf.setFillColor(colors.Color(1, 1, 1, alpha=0.12) if dark else colors.Color(1, 1, 1, alpha=0.62))
        pdf.circle(width * 0.84, height * 0.68, 145, fill=1, stroke=0)

        left = 0.62 * inch
        top = height - 0.68 * inch
        pdf.setFillColor(muted)
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(left, top, (slide.kicker or f"Slide {index + 1}").upper()[:72])
        pdf.drawRightString(width - 0.62 * inch, top, f"{index + 1} / {total}")

        title_size = 42 if len(slide.title) <= 34 else 34
        title_lines = self._wrap_text(slide.title, 24 if title_size >= 40 else 32, max_lines=3)
        y = top - 0.58 * inch
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica-Bold", title_size)
        for line in title_lines:
            pdf.drawString(left, y, line)
            y -= title_size * 1.06

        if slide.layout == "lead" and slide.body:
            pdf.setFillColor(muted)
            pdf.setFont("Helvetica", 18)
            for line in self._wrap_text(slide.body[0], 34, max_lines=2):
                pdf.drawString(left, y - 8, line)
                y -= 24
            y -= 18

        items = (slide.bullets or slide.body or [])[:4]
        if slide.layout in {"highlights", "agenda", "metrics"} and items:
            self._draw_slide_cards(pdf, items, left, 0.82 * inch, width * 0.50, max(y - 0.2 * inch, 2.8 * inch), dark)
        elif items:
            pdf.setFont("Helvetica", 16)
            pdf.setFillColor(ink)
            for item in items:
                label, detail = self._split_card_item(item)
                text = detail if detail and detail != "Ponto principal do slide" else label
                for line_index, line in enumerate(self._wrap_text(text, 58, max_lines=2)):
                    if line_index == 0:
                        pdf.setFillColor(accent)
                        pdf.circle(left + 7, y - 5, 4, fill=1, stroke=0)
                        pdf.setFillColor(ink)
                    pdf.drawString(left + 24, y - 12, line)
                    y -= 22
                y -= 10

    def _draw_slide_cards(self, pdf: Any, items: list[str], left: float, bottom: float, width: float, top: float, dark: bool) -> None:
        card_gap = 0.22 * inch
        card_w = (width - card_gap) / 2
        card_h = max(1.08 * inch, (top - bottom - card_gap) / 2)
        ink = colors.HexColor("#0c2848")
        muted = colors.HexColor("#49637f")
        accent = colors.HexColor("#1f5ea8")
        for idx, item in enumerate(items[:4]):
            col = idx % 2
            row = idx // 2
            x = left + col * (card_w + card_gap)
            y = top - (row + 1) * card_h - row * card_gap
            pdf.setFillColor(colors.white if not dark else colors.HexColor("#f7fbff"))
            pdf.roundRect(x, y, card_w, card_h, 14, fill=1, stroke=0)
            label, detail = self._split_card_item(item)
            pdf.setFillColor(accent)
            pdf.setFont("Helvetica-Bold", 8)
            pdf.drawString(x + 18, y + card_h - 24, label.upper()[:34])
            pdf.setFillColor(ink)
            pdf.setFont("Helvetica-Bold", 18)
            text = detail if detail and detail != "Ponto principal do slide" else label
            text_lines = self._wrap_text(text, 22, max_lines=3)
            line_y = y + card_h - 52
            for line in text_lines:
                pdf.drawString(x + 18, line_y, line)
                line_y -= 21
            pdf.setFillColor(muted)
            pdf.setFont("Helvetica", 9)
            support = self._short_support_copy(text)
            for line in self._wrap_text(support, 30, max_lines=2):
                pdf.drawString(x + 18, max(y + 18, line_y - 4), line)
                line_y -= 12

    def _wrap_text(self, text: str, max_chars: int, max_lines: int = 4) -> list[str]:
        words = self._clean_inline_markdown(text).split()
        lines: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join(current + [word])
            if current and len(candidate) > max_chars:
                lines.append(" ".join(current))
                current = [word]
                if len(lines) >= max_lines:
                    break
            else:
                current.append(word)
        if current and len(lines) < max_lines:
            lines.append(" ".join(current))
        if len(lines) == max_lines and len(" ".join(words)) > len(" ".join(lines)):
            lines[-1] = lines[-1].rstrip(".,;:") + "."
        return lines or [""]

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
        card_like_bullets = [
            line
            for line in slide.splitlines()
            if re.match(r"^[-*]\s+.+(?:\s[-:]\s+|\s:\s+).+", line.strip())
        ]
        if len(card_like_bullets) >= 3:
            return "highlights"
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

    def _render_slide_pdf(self, markdown_path: Path, html_path: Path, pdf_path: Path, html_text: str | None = None) -> str:
        html_payload = html_text or self._inline_slide_assets(html_path.read_text(encoding="utf-8"), html_path.parent)
        if self.settings.gotenberg_url and html_path.exists():
            try:
                self._build_pdf_with_gotenberg(pdf_path, markdown_path.stem, html_payload, slide_layout=True)
                return "presentation-html + gotenberg"
            except Exception:
                pass
        return "presentation-html-unavailable"

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
        text = re.sub(r"\b(pdf|docx|arquivo|documento|word|markdown|slide|slides|deck|pptx?|apresenta[cç][aã]o)\b", "", text, flags=re.I)
        text = re.split(
            r"\b(citando|com|incluindo|inclua|usando|use|no estilo|em estilo|formato|de forma|para|nivel|nível)\b",
            text,
            maxsplit=1,
            flags=re.I,
        )[0]
        text = re.sub(r"\b(suas|devidas|devida|devido)\b", "", text, flags=re.I)
        text = re.sub(r"\bisos?\b", "ISO", text, flags=re.I)
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
                    return self._polish_topic_title(topic)
        return self._polish_topic_title(text[:90] or "Documento solicitado")

    def _polish_topic_title(self, topic: str) -> str:
        cleaned = self._clean_inline_markdown(topic).strip(" .:-")
        replacements = {
            "gestao de ativos na tecnologia da informacao": "Gestao de Ativos de TI",
            "gestão de ativos na tecnologia da informação": "Gestao de Ativos de TI",
            "gestao de ativos em ti": "Gestao de Ativos de TI",
            "gestão de ativos em ti": "Gestao de Ativos de TI",
        }
        lowered = cleaned.lower()
        for needle, replacement in replacements.items():
            if needle in lowered:
                return replacement
        small_words = {"de", "da", "do", "das", "dos", "e", "em", "na", "no", "para", "com"}
        words: list[str] = []
        for word in cleaned.split():
            if word.upper() in {"TI", "ISO", "ISOS", "LGPD", "IA"}:
                words.append(word.upper().replace("ISOS", "ISO"))
            elif word.lower() in small_words:
                words.append(word.lower())
            else:
                words.append(word[:1].upper() + word[1:].lower())
        return " ".join(words)[:80] or "Apresentacao"

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
        if self._is_slide_request(user_request):
            source_text = "\n".join(
                f"## {block.text}" if block.kind == "heading" else f"- {block.text}" if block.kind in {"bullet", "numbered"} else block.text
                for block in blocks
                if block.text
            )
            normalized_source = self._fallback_slide_deck_text(user_request, title) if self._needs_slide_fallback(source_text, user_request) else self._normalize_slide_source(source_text)
            raw_slides = self._split_raw_slides(normalized_source) if self._looks_like_marp_deck(normalized_source) else self._split_into_slides(normalized_source)
            if len(raw_slides) < 6:
                raw_slides = self._split_into_slides(self._fallback_slide_deck_text(user_request, title))
            slides = self._build_slide_models(raw_slides, title, {})
            html_path = path.with_suffix(".html")
            markdown_path = path.with_suffix(".md")
            html_path.write_text(self._build_presentation_html(title, slides, user_request), encoding="utf-8")
            markdown_path.write_text(self._build_marp_markdown(title, normalized_source), encoding="utf-8")
            return self._build_presentation_pdf(markdown_path, html_path, path, slides)
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
        if self._is_slide_request(user_request):
            slide_blocks = self._group_slides(title, blocks)
            slides = [
                Slide(
                    title=item["title"],
                    body=[entry["text"] for entry in item["items"] if entry["kind"] != "bullet"] or None,
                    bullets=[entry["text"] for entry in item["items"] if entry["kind"] == "bullet"] or None,
                    kicker=f"Slide {index + 1}",
                )
                for index, item in enumerate(slide_blocks)
            ]
            self._build_slide_pdf_with_reportlab(path, slides)
            return
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

    def _build_slide_pdf_with_reportlab_legacy(self, path: Path, title: str, blocks: list[Block]) -> None:
        styles = getSampleStyleSheet()
        slide_title = ParagraphStyle(
            "KimiSlideTitle",
            parent=styles["Title"],
            fontName="Helvetica-Bold",
            fontSize=24,
            leading=28,
            textColor=colors.HexColor("#0f172a"),
            spaceAfter=14,
        )
        slide_body = ParagraphStyle(
            "KimiSlideBody",
            parent=styles["BodyText"],
            fontName="Helvetica",
            fontSize=14,
            leading=20,
            textColor=colors.HexColor("#334155"),
            spaceAfter=10,
        )
        slides = self._group_slides(title, blocks)
        story: list[Any] = []
        for index, slide in enumerate(slides):
            story.append(Paragraph(slide["title"], slide_title))
            for item in slide["items"]:
                prefix = "• " if item["kind"] == "bullet" else ""
                story.append(Paragraph(f"{prefix}{item['text']}", slide_body))
            if index < len(slides) - 1:
                from reportlab.platypus import PageBreak

                story.append(PageBreak())

        pdf = SimpleDocTemplate(
            str(path),
            pagesize=landscape(LETTER),
            leftMargin=0.75 * inch,
            rightMargin=0.75 * inch,
            topMargin=0.65 * inch,
            bottomMargin=0.65 * inch,
        )
        pdf.build(story)

    def _build_pdf_with_gotenberg(self, path: Path, title: str, html: str, slide_layout: bool = False) -> None:
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
        if slide_layout:
            data.update(
                {
                    "generateDocumentOutline": "false",
                    "preferCssPageSize": "true",
                    "paperWidth": "13.333",
                    "paperHeight": "7.5",
                    "marginTop": "0",
                    "marginBottom": "0",
                    "marginLeft": "0",
                    "marginRight": "0",
                    "emulatedMediaType": "screen",
                    "scale": "1.0",
                }
            )
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
        if self._is_slide_request(user_request):
            return self._build_slide_html(title, blocks)
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

    def _build_slide_html(self, title: str, blocks: list[Block]) -> str:
        slides = self._group_slides(title, blocks)
        slides_markup = []
        for index, slide in enumerate(slides, start=1):
            items_markup = []
            for item in slide["items"]:
                tag = "li" if item["kind"] == "bullet" else "p"
                items_markup.append(f"<{tag}>{self._escape_html(item['text'])}</{tag}>")
            body_markup = "".join(items_markup) or "<p>Conteudo em preparacao.</p>"
            list_open = "<ul>" if any(item["kind"] == "bullet" for item in slide["items"]) else ""
            list_close = "</ul>" if any(item["kind"] == "bullet" for item in slide["items"]) else ""
            if list_open:
                bullets = "".join(f"<li>{self._escape_html(item['text'])}</li>" for item in slide["items"] if item["kind"] == "bullet")
                paragraphs = "".join(f"<p>{self._escape_html(item['text'])}</p>" for item in slide["items"] if item["kind"] != "bullet")
                body_markup = f"{paragraphs}{list_open}{bullets}{list_close}"
            slides_markup.append(
                f"""
  <section class="slide">
    <div class="slide-chrome">
      <span class="slide-index">{index:02d}</span>
      <span class="slide-mark">Kimi deck</span>
    </div>
    <div class="slide-body">
      <h1>{self._escape_html(slide["title"])}</h1>
      <div class="slide-content">{body_markup}</div>
    </div>
  </section>"""
            )
        return f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <title>{self._escape_html(title)}</title>
  <style>
    @page {{
      size: 13.333in 7.5in;
      margin: 0;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: #f4efe6;
      color: #111827;
    }}
    .slide {{
      width: 13.333in;
      height: 7.5in;
      padding: 0.5in;
      page-break-after: always;
      background:
        radial-gradient(circle at top left, rgba(52, 211, 153, 0.12), transparent 24%),
        linear-gradient(135deg, #fffdf8 0%, #f5efe5 100%);
    }}
    .slide:last-child {{
      page-break-after: auto;
    }}
    .slide-chrome {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 0.35in;
      color: #6b7280;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
    }}
    .slide-body {{
      display: grid;
      gap: 0.26in;
      height: calc(100% - 0.7in);
      align-content: start;
      padding: 0.18in 0.08in;
    }}
    h1 {{
      margin: 0;
      font-size: 32px;
      line-height: 1.05;
      color: #0f172a;
    }}
    .slide-content {{
      display: grid;
      gap: 0.12in;
      font-size: 18px;
      line-height: 1.45;
      color: #334155;
    }}
    p {{
      margin: 0;
    }}
    ul {{
      margin: 0;
      padding-left: 0.24in;
    }}
    li {{
      margin: 0 0 0.08in;
    }}
  </style>
</head>
<body>
{''.join(slides_markup)}
</body>
</html>"""

    def _group_slides(self, title: str, blocks: list[Block]) -> list[dict[str, Any]]:
        slides: list[dict[str, Any]] = []
        current = {"title": title, "items": []}
        for block in blocks:
            if block.kind == "heading" and block.level <= 2:
                if current["items"]:
                    slides.append(current)
                current = {"title": block.text, "items": []}
                continue
            current["items"].append({"kind": "bullet" if block.kind == "bullet" else "text", "text": block.text})
            if len(current["items"]) >= 5:
                slides.append(current)
                current = {"title": title, "items": []}
        if current["items"] or not slides:
            slides.append(current)
        return slides

    def _is_slide_request_legacy(self, user_request: str) -> bool:
        lowered = " ".join(user_request.split()).lower()
        slide_markers = [
            "slide",
            "slides",
            "apresentacao",
            "apresentação",
            "deck",
            "pitch deck",
            "powerpoint",
            "ppt",
            "pptx",
        ]
        return any(marker in lowered for marker in slide_markers)

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
        cleaned = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", cleaned)
        cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
        cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", cleaned)
        cleaned = re.sub(r"^#{1,6}\s*", "", cleaned)
        cleaned = re.sub(r"^>\s*", "", cleaned)
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
