from __future__ import annotations

import html
import re
from dataclasses import dataclass


ARTIFACT_PATTERN = re.compile(
    r"<kemy_artifact\b(?:[^>]*\btitle\s*=\s*(?P<quote>[\"'])(?P<title>.*?)(?P=quote))?[^>]*>(?P<body>.*?)</kemy_artifact\s*>",
    re.IGNORECASE | re.DOTALL,
)
FILE_PATTERN = re.compile(
    r"<file\b[^>]*\bpath\s*=\s*(?P<quote>[\"'])(?P<path>.*?)(?P=quote)[^>]*>(?P<content>.*?)</file\s*>",
    re.IGNORECASE | re.DOTALL,
)
EXECUTE_CLOUD_PATTERN = re.compile(
    r"<execute_cloud\s+tool=\"(?P<tool>[^\"]+)\"\s*>(?P<payload>.*?)</execute_cloud>",
    re.IGNORECASE | re.DOTALL,
)
EXECUTE_SANDBOX_PATTERN = re.compile(
    r"<execute_sandbox\s+lang=\"(?P<lang>[^\"]+)\"\s*>(?P<code>.*?)</execute_sandbox>",
    re.IGNORECASE | re.DOTALL,
)
FINAL_ANSWER_PATTERN = re.compile(
    r"<final_answer>\s*(?P<answer>.*?)</final_answer>",
    re.IGNORECASE | re.DOTALL,
)
TOOL_RESULT_PATTERN = re.compile(
    r"<tool_result>\s*(?P<result>.*?)</tool_result>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ArtifactFile:
    path: str
    content: str


@dataclass(frozen=True)
class ParsedArtifact:
    title: str
    files: list[ArtifactFile]


@dataclass(frozen=True)
class RoutedAction:
    kind: str
    payload: str
    tool: str = ""
    language: str = "python"


def parse_kemy_artifact(raw: str) -> ParsedArtifact | None:
    if not raw:
        return None
    raw = html.unescape(raw)
    match = ARTIFACT_PATTERN.search(raw)
    if not match:
        return None
    body = match.group("body") or ""
    files = [
        ArtifactFile(path=item.group("path").strip(), content=(item.group("content") or "").strip())
        for item in FILE_PATTERN.finditer(body)
        if item.group("path")
    ]
    if not files:
        return None
    title = (match.group("title") or "Kemy Artifact").strip()
    return ParsedArtifact(title=title, files=files)


def strip_artifact_wrapper(raw: str) -> str:
    parsed = parse_kemy_artifact(raw)
    if not parsed:
        return raw
    return "\n\n".join(file.content for file in parsed.files[:1]).strip()


def detect_agentic_action(raw: str) -> RoutedAction | None:
    if not raw:
        return None
    final = FINAL_ANSWER_PATTERN.search(raw)
    if final:
        return RoutedAction(kind="final_answer", payload=(final.group("answer") or "").strip())
    sandbox = EXECUTE_SANDBOX_PATTERN.search(raw)
    if sandbox:
        return RoutedAction(
            kind="execute_sandbox",
            payload=(sandbox.group("code") or "").strip(),
            language=(sandbox.group("lang") or "python").strip().lower(),
        )
    cloud = EXECUTE_CLOUD_PATTERN.search(raw)
    if cloud:
        return RoutedAction(
            kind="execute_cloud",
            payload=(cloud.group("payload") or "").strip(),
            tool=(cloud.group("tool") or "").strip(),
        )
    return None


def extract_tool_results(raw: str) -> list[str]:
    return [(item.group("result") or "").strip() for item in TOOL_RESULT_PATTERN.finditer(raw)]
