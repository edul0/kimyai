from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import Storage
from .artifact_parser import parse_kemy_artifact


TOKEN_PATTERN = re.compile(r"[A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_]{2,}")


class LocalSemanticSearch:
    """Dependency-free TF-IDF retrieval; deterministic fallback when Ollama embeddings are absent."""

    def rank(self, query: str, documents: list[dict[str, str]], limit: int = 12) -> list[dict[str, Any]]:
        tokenized = [self._tokens(item.get("content", "") + " " + item.get("path", "")) for item in documents]
        query_tokens = self._tokens(query)
        if not query_tokens:
            return []
        document_frequency = Counter(token for tokens in tokenized for token in set(tokens))
        count = max(len(documents), 1)
        ranked = []
        for item, tokens in zip(documents, tokenized):
            frequencies = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                tf = frequencies[token] / max(len(tokens), 1)
                idf = math.log((count + 1) / (document_frequency[token] + 1)) + 1
                score += tf * idf
            if score > 0:
                ranked.append({**item, "semantic_score": round(score, 6)})
        return sorted(ranked, key=lambda item: (-item["semantic_score"], item.get("path", "")))[:limit]

    @staticmethod
    def _tokens(value: str) -> list[str]:
        return [token.lower() for token in TOKEN_PATTERN.findall(value)]


class ImpactGraph:
    def build(self, dependencies: dict[str, list[str]], symbols: list[dict[str, Any]]) -> dict[str, Any]:
        reverse: dict[str, list[str]] = {}
        for source, imports in dependencies.items():
            for imported in imports:
                normalized = imported.lstrip(".").replace(".", "/")
                reverse.setdefault(normalized, []).append(source)
        symbol_files: dict[str, list[str]] = {}
        for symbol in symbols:
            symbol_files.setdefault(str(symbol.get("name") or ""), []).append(str(symbol.get("path") or ""))
        return {"imports": dependencies, "reverse_imports": reverse, "symbol_files": symbol_files}

    def impacted_files(self, changed_paths: list[str], graph: dict[str, Any]) -> list[str]:
        impacted = set(changed_paths)
        reverse = graph.get("reverse_imports") or {}
        for changed in changed_paths:
            module = changed.rsplit(".", 1)[0]
            for key, consumers in reverse.items():
                if module.endswith(key) or key.endswith(module):
                    impacted.update(consumers)
        return sorted(impacted)


@dataclass
class EvidenceConfidence:
    level: str
    score: float
    verified_by: list[str]
    unverified: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level, "score": self.score,
            "verified_by": self.verified_by, "unverified": self.unverified,
        }


def confidence_from_evidence(
    static_ok: bool,
    browser: dict[str, Any] | None = None,
    sandbox: dict[str, Any] | None = None,
) -> EvidenceConfidence:
    score = 0.15
    verified = []
    unverified = []
    if static_ok:
        score += 0.3
        verified.append("analise-estatica")
    else:
        unverified.append("corretude-estatica")
    if browser and browser.get("available"):
        if browser.get("ok"):
            score += 0.25
            verified.append("navegador-real")
        else:
            unverified.append("frontend-no-navegador")
    else:
        unverified.append("navegador-real")
    if sandbox and sandbox.get("available"):
        if sandbox.get("ok"):
            score += 0.3
            verified.append("testes-em-sandbox-forte")
        else:
            unverified.append("testes-em-sandbox-forte")
    else:
        unverified.append("testes-em-sandbox-forte")
    score = round(min(score, 1.0), 2)
    level = "high" if score >= 0.8 else "medium" if score >= 0.5 else "low"
    return EvidenceConfidence(level, score, verified, unverified)


class SolutionHistory:
    def __init__(self, storage: Storage, ttl_seconds: int = 2_592_000):
        self.storage = storage
        self.ttl_seconds = ttl_seconds

    def record(self, project_id: str, entry: dict[str, Any]) -> None:
        key = f"solution-history:{project_id}"
        items = self.storage.get_json(key, [])
        fingerprint = str(entry.get("fingerprint") or "")
        if fingerprint:
            items = [item for item in items if item.get("fingerprint") != fingerprint]
        items.append(entry)
        self.storage.set_json(key, items[-80:], ttl=self.ttl_seconds)

    def relevant(self, project_id: str, request: str, limit: int = 6) -> list[dict[str, Any]]:
        items = self.storage.get_json(f"solution-history:{project_id}", [])
        documents = [
            {"path": str(index), "content": f"{item.get('request', '')} {item.get('errors', '')}", "entry": item}
            for index, item in enumerate(items)
        ]
        return [item["entry"] for item in LocalSemanticSearch().rank(request, documents, limit=limit)]


class StrongSandboxRuntime:
    """Runs known validation commands only inside Docker with network disabled."""

    ALLOWED = {
        "python -m pytest -q": ["python", "-m", "pytest", "-q"],
        "python -m compileall -q .": ["python", "-m", "compileall", "-q", "."],
        "npm test -- --run": ["npm", "test", "--", "--run"],
        "npm run build": ["npm", "run", "build"],
    }

    def available(self) -> bool:
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            result = subprocess.run(
                [docker, "info"], capture_output=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def run(self, workspace: str | Path, command: str, timeout: int = 120) -> dict[str, Any]:
        if command not in self.ALLOWED:
            return {"available": self.available(), "ok": False, "error": "Comando fora da allowlist."}
        if not self.available():
            return {"available": False, "ok": False, "error": "Docker indisponivel."}
        docker = shutil.which("docker")
        root = Path(workspace).resolve()
        image = "python:3.12-slim" if command.startswith("python") else "node:22-slim"
        image_check = subprocess.run(
            [docker, "image", "inspect", image], capture_output=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if image_check.returncode:
            return {
                "available": False, "ok": False,
                "error": f"Imagem Docker local `{image}` ausente; download automatico nao autorizado.",
            }
        args = [
            docker, "run", "--rm", "--network", "none", "--read-only",
            "--memory", "768m", "--cpus", "1.5", "--pids-limit", "256",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "-v", f"{root}:/workspace:ro", "-w", "/workspace", image,
            *self.ALLOWED[command],
        ]
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return {
                "available": True, "ok": result.returncode == 0, "command": command,
                "returncode": result.returncode,
                "output": ((result.stdout or "") + "\n" + (result.stderr or "")).strip()[:8000],
            }
        except subprocess.TimeoutExpired:
            return {"available": True, "ok": False, "command": command, "error": "Timeout."}

    def validate_artifact(
        self,
        raw: str,
        base_root: str | Path | None = None,
        existing_paths: set[str] | None = None,
    ) -> dict[str, Any]:
        artifact = parse_kemy_artifact(raw)
        if not artifact:
            return {"available": self.available(), "ok": False, "error": "Artifact ausente."}
        with tempfile.TemporaryDirectory(prefix="kemy-strong-sandbox-") as temp:
            workspace = Path(temp).resolve()
            source_root = Path(base_root).resolve() if base_root else None
            if source_root and source_root.is_dir():
                for relative in sorted(existing_paths or set()):
                    source = (source_root / relative).resolve()
                    if source_root not in source.parents or not source.is_file():
                        continue
                    destination = workspace / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(source, destination)
                    except OSError:
                        continue
            for item in artifact.files:
                target = (workspace / item.path.replace("\\", "/").lstrip("/")).resolve()
                if workspace not in target.parents:
                    return {"available": self.available(), "ok": False, "error": f"Caminho inseguro: {item.path}"}
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item.content, encoding="utf-8")
            if any(workspace.rglob("*.py")):
                return self.run(workspace, "python -m compileall -q .")
            return {
                "available": self.available(), "ok": False,
                "error": "Nenhum comando forte aplicavel sem instalar dependencias ou executar codigo nao confiavel.",
            }


class GitWorktreeEditor:
    """Opt-in isolated editor. It never stages, commits, pushes, or removes the worktree."""

    def prepare(self, repository: str | Path, branch_prefix: str = "kemy/task") -> dict[str, Any]:
        root = Path(repository).resolve()
        git = shutil.which("git")
        if not git or not (root / ".git").exists():
            return {"ok": False, "error": "Repositorio Git indisponivel."}
        target = Path(tempfile.mkdtemp(prefix="kemy-worktree-")).resolve()
        branch = f"{branch_prefix}-{target.name.rsplit('-', 1)[-1]}"
        safe_root = str(root).replace("\\", "/")
        command = [
            git, "-c", f"safe.directory={safe_root}", "-C", str(root),
            "worktree", "add", "-b", branch, str(target),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            return {"ok": False, "error": (result.stderr or result.stdout).strip()[:1200]}
        return {
            "ok": True, "path": str(target), "branch": branch,
            "next_action": "Aplicar arquivos, revisar diff e validar. Commit/push continuam dependentes de autorizacao.",
        }

    def diff(self, worktree: str | Path) -> dict[str, Any]:
        root = Path(worktree).resolve()
        git = shutil.which("git")
        if not git:
            return {"ok": False, "error": "Git indisponivel."}
        safe_root = str(root).replace("\\", "/")
        result = subprocess.run(
            [git, "-c", f"safe.directory={safe_root}", "-C", str(root), "diff", "--no-ext-diff"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": result.returncode == 0, "diff": result.stdout[:40_000], "error": result.stderr[:1200]}
