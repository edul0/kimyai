from __future__ import annotations

import base64
import io
import re
from typing import Any

from docx import Document
from pypdf import PdfReader


def _compact_text(value: str, limit: int = 12000) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _decode_data_url(content: str) -> tuple[str | None, bytes | None]:
    if not content.startswith("data:"):
        return None, None
    match = re.match(r"^data:([^;]+);base64,(.+)$", content, re.I | re.S)
    if not match:
        return None, None
    mime_type = match.group(1).strip().lower()
    try:
        payload = base64.b64decode(match.group(2), validate=False)
    except Exception:
        return mime_type, None
    return mime_type, payload


def _extract_pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    parts: list[str] = []
    for page in reader.pages[:20]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return _compact_text("\n".join(parts), 18000)


def _extract_docx_text(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
    return _compact_text("\n".join(parts), 18000)


def prepare_attachments(attachments: list[dict[str, Any]] | None) -> dict[str, Any]:
    prepared: list[dict[str, Any]] = []
    prompt_sections: list[str] = []
    visual_items: list[dict[str, Any]] = []

    for item in attachments or []:
        name = str(item.get("name") or "anexo").strip()[:180]
        declared_mime = str(item.get("mime_type") or "text/plain").strip().lower()
        kind = str(item.get("kind") or "text").strip().lower()
        content = str(item.get("content") or "")

        resolved_mime, binary = _decode_data_url(content)
        mime_type = resolved_mime or declared_mime
        extracted_text = ""
        notes: list[str] = []

        if mime_type == "application/pdf" and binary:
            extracted_text = _extract_pdf_text(binary)
            notes.append("pdf")
        elif mime_type in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/msword",
        } and binary:
            extracted_text = _extract_docx_text(binary)
            notes.append("docx")
        elif kind == "text":
            extracted_text = _compact_text(content, 18000)
            notes.append("text")
        elif mime_type.startswith("image/") and binary:
            notes.append("image")
            visual_items.append(
                {
                    "name": name,
                    "mime_type": mime_type,
                    "data": binary,
                    "kind": "image",
                }
            )
        elif binary:
            notes.append("binary")

        if mime_type == "application/pdf" and binary:
            visual_items.append(
                {
                    "name": name,
                    "mime_type": mime_type,
                    "data": binary,
                    "kind": "document",
                }
            )

        prepared_item = {
            "name": name,
            "mime_type": mime_type,
            "kind": kind,
            "bytes": binary,
            "text": extracted_text,
            "notes": notes,
            "visual": bool(visual_items and visual_items[-1]["name"] == name),
        }
        prepared.append(prepared_item)

        if extracted_text:
            prompt_sections.append(
                f"## {name} ({mime_type})\nTexto extraido do anexo:\n{_compact_text(extracted_text, 6000)}"
            )
        elif mime_type.startswith("image/"):
            prompt_sections.append(
                f"## {name} ({mime_type})\nImagem anexada. Analise visual necessaria para descrever a cena e transcrever o texto visivel."
            )
        elif mime_type == "application/pdf":
            prompt_sections.append(
                f"## {name} ({mime_type})\nPDF anexado. Se a extracao local vier incompleta, complemente pela leitura visual do documento."
            )
        else:
            prompt_sections.append(f"## {name} ({mime_type})\nArquivo anexado.")

    return {
        "items": prepared,
        "prompt_context": "\n\n".join(prompt_sections).strip(),
        "visual_items": visual_items,
        "has_visual": bool(visual_items),
    }
