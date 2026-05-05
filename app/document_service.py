from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
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


class DocumentService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.output_root = Path("generated_documents")

    def generate(self, session_id: str, job_id: str, user_request: str, draft: dict[str, Any]) -> dict[str, Any]:
        source_text = self._source_text(user_request, draft)
        title = self._title_from_text(user_request, source_text)
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

    def _source_text(self, user_request: str, draft: dict[str, Any]) -> str:
        for key in ("raw", "summary"):
            value = str(draft.get(key) or "").strip()
            cleaned = self._normalize_document_text(self._strip_system_notices(value), user_request)
            if self._looks_like_document_content(cleaned):
                return cleaned
        return self._fallback_document_text(user_request)

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

        def flush_buffers() -> None:
            nonlocal bullet_buffer, numbered_buffer
            for item in bullet_buffer:
                blocks.append(Block("bullet", item))
            for item in numbered_buffer:
                blocks.append(Block("numbered", item))
            bullet_buffer = []
            numbered_buffer = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                flush_buffers()
                continue
            if line.startswith("```"):
                continue
            heading = re.match(r"^(#{1,3})\s+(.*)$", line)
            if heading:
                flush_buffers()
                blocks.append(Block("heading", heading.group(2).strip(), len(heading.group(1))))
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
            blocks.append(Block("paragraph", line))

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

        doc.add_paragraph(title, style="Title")
        doc.add_paragraph()

        for block in blocks:
            if block.kind == "heading":
                style_name = {1: "Heading 1", 2: "Heading 2", 3: "Heading 3"}.get(block.level, "Heading 2")
                doc.add_paragraph(block.text, style=style_name)
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
        h1 = ParagraphStyle("KimiH1", parent=styles["Heading1"], fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=colors.HexColor("#14213d"), spaceBefore=10, spaceAfter=8)
        h2 = ParagraphStyle("KimiH2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, leading=17, textColor=colors.HexColor("#223b63"), spaceBefore=8, spaceAfter=6)
        title_style = ParagraphStyle("KimiTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=22, leading=26, textColor=colors.HexColor("#0f172a"), spaceAfter=8)
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
            else:
                story.append(Paragraph(block.text, body))
        flush_lists()

        pdf = SimpleDocTemplate(str(path), pagesize=LETTER, leftMargin=inch, rightMargin=inch, topMargin=inch, bottomMargin=inch)
        pdf.build(story)

    def _build_pdf_with_gotenberg(self, path: Path, title: str, html: str) -> None:
        base_url = (self.settings.gotenberg_url or "").rstrip("/")
        if not base_url:
            raise ValueError("Gotenberg URL nao configurada.")
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
