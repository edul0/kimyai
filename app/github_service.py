from __future__ import annotations
import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

class GitHubService:
    def __init__(self, token: str | None, default_repo: str | None, default_branch: str = "main",
                 user_name: str = "Kemy AI", user_email: str = "kemy@ai.local"):
        self.token = token
        self.default_repo = default_repo
        self.default_branch = default_branch
        self.user_name = user_name
        self.user_email = user_email

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def _inject_token(self, url: str) -> str:
        if not self.token: return url
        url = url.strip()
        if url.startswith("https://") and "@" not in url:
            url = url.replace("https://", f"https://{self.token}@")
        return url

    def _repo_dir(self, repo_url: str) -> Path:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_url.split("/")[-1].replace(".git", ""))
        base = Path(tempfile.gettempdir()) / "kemy_git_repos"
        base.mkdir(parents=True, exist_ok=True)
        return base / slug

    async def _run(self, args: list[str], cwd: str | Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        merged_env = {**os.environ, **(env or {})}
        merged_env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None, env=merged_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            return {"success": proc.returncode == 0, "output": out or err, "stdout": out, "stderr": err, "returncode": proc.returncode}
        except Exception as exc:
            return {"success": False, "output": f"Erro: {exc}", "stdout": "", "stderr": "", "returncode": -1}

    async def execute_operation(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        # Minimal implementation for now to ensure stability
        return {"success": False, "output": "Operação Git em desenvolvimento nesta build estável."}
