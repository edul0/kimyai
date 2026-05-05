from __future__ import annotations

import re
from typing import Any


MAX_ITEMS = 12
MAX_TEXT = 260


def compact_text(value: Any, limit: int = MAX_TEXT) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def split_request_parts(message: str) -> list[dict[str, str]]:
    text = " ".join(message.split()).strip()
    if not text:
        return []
    chunks = re.split(r"\s+(?:e|depois|tambem|tambem quero|alem disso|mas|so que|porque|para)\s+", text, flags=re.I)
    parts = []
    for index, chunk in enumerate(chunks, start=1):
        cleaned = compact_text(chunk, 180)
        if cleaned:
            parts.append({"step": str(index), "text": cleaned, "kind": classify_part(cleaned)})
    return parts[:8]


def classify_part(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in ["nao", "nunca", "sem ", "limite", "restricao", "obrigatorio"]):
        return "constraint"
    if any(word in lowered for word in ["quero", "preciso", "implemente", "crie", "corrija", "adicione", "faca"]):
        return "requirement"
    if any(word in lowered for word in ["como", "por que", "porque", "qual", "?"]):
        return "question"
    return "context"


def _append_unique(items: list[str], value: str) -> None:
    cleaned = compact_text(value)
    if cleaned and cleaned not in items:
        items.append(cleaned)
    del items[:-MAX_ITEMS]


def _extract_named_lines(message: str) -> dict[str, list[str]]:
    lowered = message.lower()
    found: dict[str, list[str]] = {
        "requirements": [],
        "constraints": [],
        "preferences": [],
        "open_questions": [],
    }
    for part in split_request_parts(message):
        kind = part["kind"]
        text = part["text"]
        if kind == "constraint":
            found["constraints"].append(text)
        elif kind == "question":
            found["open_questions"].append(text)
        elif kind == "requirement":
            found["requirements"].append(text)
    preference_markers = ["eu quero", "prefiro", "gosto", "tem que", "deve", "sempre", "cada pergunta"]
    if any(marker in lowered for marker in preference_markers):
        found["preferences"].append(message)
    return found


def build_context_snapshot(
    previous: dict[str, Any] | None,
    user_message: str,
    assistant_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = {
        "summary": "",
        "requirements": [],
        "constraints": [],
        "preferences": [],
        "open_questions": [],
        "request_parts": [],
        "last_user_intent": "",
        "last_answer_summary": "",
    }
    if previous:
        for key in snapshot:
            value = previous.get(key)
            if isinstance(snapshot[key], list):
                snapshot[key] = list(value or [])
            else:
                snapshot[key] = str(value or "")

    extracted = _extract_named_lines(user_message)
    for key in ("requirements", "constraints", "preferences", "open_questions"):
        for item in extracted[key]:
            _append_unique(snapshot[key], item)

    parts = split_request_parts(user_message)
    if parts:
        snapshot["request_parts"] = parts
        snapshot["last_user_intent"] = compact_text(parts[0]["text"])
    else:
        snapshot["last_user_intent"] = compact_text(user_message)

    if assistant_result:
        answer = assistant_result.get("summary") or assistant_result.get("raw") or ""
        snapshot["last_answer_summary"] = compact_text(answer)

    summary_inputs = []
    if snapshot["last_user_intent"]:
        summary_inputs.append(f"Foco atual: {snapshot['last_user_intent']}")
    if snapshot["requirements"]:
        summary_inputs.append("Requisitos: " + "; ".join(snapshot["requirements"][-4:]))
    if snapshot["constraints"]:
        summary_inputs.append("Restricoes: " + "; ".join(snapshot["constraints"][-4:]))
    if snapshot["preferences"]:
        summary_inputs.append("Preferencias: " + "; ".join(snapshot["preferences"][-3:]))
    snapshot["summary"] = compact_text(" | ".join(summary_inputs), 900)
    return snapshot


def context_to_prompt(context: dict[str, Any] | None) -> str:
    if not context:
        return "- sem contexto compactado ainda"
    lines = [context.get("summary") or "- sem resumo"]
    parts = context.get("request_parts") or []
    if parts:
        lines.append("Partes do ultimo pedido:")
        lines.extend(f"- {item.get('kind')}: {item.get('text')}" for item in parts[:8])
    open_questions = context.get("open_questions") or []
    if open_questions:
        lines.append("Perguntas/pendencias abertas:")
        lines.extend(f"- {item}" for item in open_questions[-5:])
    return "\n".join(lines)
