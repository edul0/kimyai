from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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


@dataclass
class Block:
    kind: str
    text: str
    level: int = 0


class DocumentService:
    def __init__(self) -> None:
        self.output_root = Path("generated_documents")

    def generate(self, session_id: str, job_id: str, user_request: str, draft: dict[str, Any]) -> dict[str, Any]:
        source_text = self._source_text(user_request, draft)
        title = self._title_from_text(user_request, source_text)
        blocks = self._parse_blocks(source_text)
        folder = self.output_root / session_id / job_id
        folder.mkdir(parents=True, exist_ok=True)

        docx_name = self._safe_filename(title, ".docx")
        pdf_name = self._safe_filename(title, ".pdf")
        docx_path = folder / docx_name
        pdf_path = folder / pdf_name

        self._build_docx(docx_path, title, blocks, user_request)
        self._build_pdf(pdf_path, title, blocks, user_request)

        summary = "Documento organizado em DOCX e PDF gerado com sucesso."
        raw = (
            f"Arquivos gerados com sucesso: `{docx_name}` e `{pdf_name}`.\n\n"
            "Use os links do chat para abrir ou baixar o DOCX e o PDF."
        )
        return {
            "provider": draft.get("provider", "kimi-documento"),
            "model": draft.get("model", "python-docx+reportlab"),
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
            if value:
                return value
        return (
            f"# Documento solicitado\n\n"
            f"## Objetivo\n{user_request.strip()}\n\n"
            "## Estrutura sugerida\n"
            "- Introducao\n"
            "- Desenvolvimento\n"
            "- Proximos passos\n"
        )

    def _title_from_text(self, user_request: str, text: str) -> str:
        for line in text.splitlines():
            cleaned = re.sub(r"^#+\s*", "", line).strip()
            if len(cleaned) >= 6:
                return cleaned[:80]
        base = " ".join(user_request.split()).strip()
        return (base[:80] or "Documento Kimi AI").rstrip(" .:-")

    def _safe_filename(self, title: str, extension: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
        return f"{slug or 'documento-kimi-ai'}{extension}"

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
        self._configure_header(section, "Kimi AI Documento")
        self._configure_footer(section)

        doc.add_paragraph(title, style="Title")
        subtitle = doc.add_paragraph("Entrega automatica em DOCX e PDF pelo Kimi AI", style="Subtitle")
        subtitle.alignment = WD_ALIGN_PARAGRAPH.LEFT

        meta = doc.add_table(rows=3, cols=2)
        meta.style = "Table Grid"
        meta.autofit = True
        meta.cell(0, 0).text = "Solicitacao"
        meta.cell(0, 1).text = "Documento gerado"
        meta.cell(1, 0).text = "Sessao"
        meta.cell(1, 1).text = path.parent.parent.name
        meta.cell(2, 0).text = "Gerado em"
        meta.cell(2, 1).text = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        for row in meta.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    paragraph.style = doc.styles["Normal"]
        doc.add_paragraph()

        if user_request.strip():
            intro = doc.add_paragraph(style="Normal")
            intro.add_run("Pedido original: ").bold = True
            intro.add_run(" ".join(user_request.split())[:800])

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

    def _build_pdf(self, path: Path, title: str, blocks: list[Block], user_request: str) -> None:
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
        subtitle_style = ParagraphStyle("KimiSubtitle", parent=styles["BodyText"], fontName="Helvetica", fontSize=11, leading=14, textColor=colors.HexColor("#5b6472"), spaceAfter=18)

        story: list[Any] = [
            Paragraph(title, title_style),
            Paragraph("Entrega automatica em DOCX e PDF pelo Kimi AI", subtitle_style),
        ]

        meta = Table(
            [
                ["Solicitacao", "Documento gerado"],
                ["Sessao", path.parent.parent.name],
                ["Gerado em", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")],
            ],
            colWidths=[1.7 * inch, 4.8 * inch],
        )
        meta.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#d7dde5")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef3f8")),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#243240")),
                    ("TOPPADDING", (0, 0), (-1, -1), 7),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.extend([meta, Spacer(1, 0.18 * inch)])

        if user_request.strip():
            story.append(Paragraph(f"<b>Pedido original:</b> {' '.join(user_request.split())[:800]}", body))

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
