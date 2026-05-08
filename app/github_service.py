"""GitHub integration service for Kemy AI.

Allows the AI to perform git operations (clone, pull, push, commit, etc.)
by running git commands asynchronously using the configured GitHub token.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


class GitHubService:
    """Executes git operations securely on behalf of the user."""

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
        """Inject token into HTTPS URL for authentication."""
        if not self.token:
            return url
        url = url.strip()
        if url.startswith("https://") and "@" not in url:
            url = url.replace("https://", f"https://{self.token}@")
        return url

    def _repo_dir(self, repo_url: str) -> Path:
        """Get a stable temp directory path for a given repo."""
        slug = re.sub(r"[^a-zA-Z0-9_-]", "_", repo_url.split("/")[-1].replace(".git", ""))
        base = Path(tempfile.gettempdir()) / "kemy_git_repos"
        base.mkdir(parents=True, exist_ok=True)
        return base / slug

    async def _run(self, args: list[str], cwd: str | Path | None = None,
                   env: dict[str, str] | None = None) -> dict[str, Any]:
        """Run a git command and return stdout/stderr."""
        merged_env = {**os.environ, **(env or {})}
        # Avoid interactive prompts
        merged_env["GIT_TERMINAL_PROMPT"] = "0"

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            success = proc.returncode == 0
            return {
                "success": success,
                "output": out or err,
                "stdout": out,
                "stderr": err,
                "returncode": proc.returncode,
            }
        except asyncio.TimeoutError:
            return {"success": False, "output": "Erro: comando git demorou mais de 60 segundos.", "stdout": "", "stderr": "", "returncode": -1}
        except FileNotFoundError:
            return {"success": False, "output": "Erro: git não encontrado no servidor. Verifique se o git está instalado.", "stdout": "", "stderr": "", "returncode": -1}
        except Exception as exc:
            return {"success": False, "output": f"Erro inesperado: {exc}", "stdout": "", "stderr": "", "returncode": -1}

    async def _ensure_git_config(self, path: Path) -> None:
        """Set git user config for commits."""
        await self._run(["git", "config", "user.name", self.user_name], cwd=path)
        await self._run(["git", "config", "user.email", self.user_email], cwd=path)

    async def clone(self, repo_url: str, branch: str | None = None) -> dict[str, Any]:
        """Clone a repository, or update it if already cloned."""
        branch = branch or self.default_branch
        repo_url = repo_url or self.default_repo or ""
        if not repo_url:
            return {"success": False, "output": "Nenhuma URL de repositório fornecida."}

        repo_dir = self._repo_dir(repo_url)
        auth_url = self._inject_token(repo_url)

        # If already exists, just pull
        if (repo_dir / ".git").exists():
            return await self.pull(repo_dir, branch)

        result = await self._run(["git", "clone", "--branch", branch, "--depth", "10", auth_url, str(repo_dir)])
        if result["success"]:
            await self._ensure_git_config(repo_dir)
            result["output"] = f"✅ Repositório clonado em {repo_dir}\n{result['output']}"
        return result

    async def pull(self, path: Path | str | None = None, branch: str | None = None,
                   repo_url: str | None = None) -> dict[str, Any]:
        """Pull latest changes from remote."""
        branch = branch or self.default_branch
        resolved_url = repo_url or self.default_repo or ""

        if path is None:
            path = self._repo_dir(resolved_url)

        path = Path(path)
        if not (path / ".git").exists():
            # Auto clone first
            if resolved_url:
                return await self.clone(resolved_url, branch)
            return {"success": False, "output": "Repositório não clonado e nenhuma URL fornecida."}

        # Set remote URL with token
        if resolved_url:
            auth_url = self._inject_token(resolved_url)
            await self._run(["git", "remote", "set-url", "origin", auth_url], cwd=path)

        result = await self._run(["git", "pull", "origin", branch], cwd=path)
        if result["success"]:
            result["output"] = f"✅ Pull do branch '{branch}' concluído.\n{result['output']}"
        return result

    async def status(self, path: Path | str | None = None, repo_url: str | None = None) -> dict[str, Any]:
        """Return git status."""
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            if resolved_url:
                clone_result = await self.clone(resolved_url)
                if not clone_result["success"]:
                    return clone_result
            else:
                return {"success": False, "output": "Repositório não encontrado localmente."}

        short = await self._run(["git", "status", "-sb"], cwd=path)
        full = await self._run(["git", "log", "--oneline", "-5"], cwd=path)
        output = f"📋 Status:\n{short['output']}\n\n📜 Últimos commits:\n{full['output']}"
        return {"success": True, "output": output}

    async def commit(self, message: str, files: list[str] | None = None,
                     path: Path | str | None = None, repo_url: str | None = None) -> dict[str, Any]:
        """Stage files and create a commit."""
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            return {"success": False, "output": "Repositório não encontrado. Clone primeiro."}

        await self._ensure_git_config(path)

        # Stage files
        stage_files = files if files else ["."]
        for f in stage_files:
            stage_result = await self._run(["git", "add", f], cwd=path)
            if not stage_result["success"]:
                return stage_result

        # Check if there's anything to commit
        diff_check = await self._run(["git", "diff", "--cached", "--name-only"], cwd=path)
        if not diff_check["stdout"].strip():
            return {"success": False, "output": "⚠️ Nenhuma alteração para commitar. Modifique os arquivos antes."}

        commit_result = await self._run(["git", "commit", "-m", message], cwd=path)
        if commit_result["success"]:
            commit_result["output"] = f"✅ Commit criado!\n{commit_result['output']}"
        return commit_result

    async def push(self, branch: str | None = None, path: Path | str | None = None,
                   repo_url: str | None = None) -> dict[str, Any]:
        """Push commits to remote."""
        branch = branch or self.default_branch
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            return {"success": False, "output": "Repositório não encontrado. Clone primeiro."}

        # Update remote URL with token
        if resolved_url:
            auth_url = self._inject_token(resolved_url)
            await self._run(["git", "remote", "set-url", "origin", auth_url], cwd=path)

        result = await self._run(["git", "push", "origin", branch], cwd=path)
        if result["success"]:
            result["output"] = f"✅ Push para o branch '{branch}' concluído!\n{result['output']}"
        else:
            result["output"] = f"❌ Erro no push:\n{result['output']}"
        return result

    async def list_branches(self, path: Path | str | None = None, repo_url: str | None = None) -> dict[str, Any]:
        """List all branches."""
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            if resolved_url:
                clone_result = await self.clone(resolved_url)
                if not clone_result["success"]:
                    return clone_result
            else:
                return {"success": False, "output": "Repositório não encontrado."}

        local = await self._run(["git", "branch", "-a", "-v"], cwd=path)
        return {"success": True, "output": f"🌿 Branches:\n{local['output']}"}

    async def checkout(self, branch: str, create: bool = False,
                       path: Path | str | None = None, repo_url: str | None = None) -> dict[str, Any]:
        """Switch to or create a branch."""
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            return {"success": False, "output": "Repositório não encontrado."}

        args = ["git", "checkout"]
        if create:
            args.append("-b")
        args.append(branch)
        result = await self._run(args, cwd=path)
        if result["success"]:
            result["output"] = f"✅ Mudado para o branch '{branch}'.\n{result['output']}"
        return result

    async def delete_branch(self, branch: str, remote: bool = False,
                            path: Path | str | None = None, repo_url: str | None = None) -> dict[str, Any]:
        """Delete a branch locally and/or remotely."""
        resolved_url = repo_url or self.default_repo or ""
        if path is None:
            path = self._repo_dir(resolved_url)
        path = Path(path)

        if not (path / ".git").exists():
            return {"success": False, "output": "Repositório não encontrado."}

        outputs = []
        # Delete locally
        local_result = await self._run(["git", "branch", "-D", branch], cwd=path)
        outputs.append(f"Local: {local_result['output']}")

        # Delete remotely
        if remote:
            if resolved_url:
                auth_url = self._inject_token(resolved_url)
                await self._run(["git", "remote", "set-url", "origin", auth_url], cwd=path)
            remote_result = await self._run(["git", "push", "origin", "--delete", branch], cwd=path)
            outputs.append(f"Remoto: {remote_result['output']}")

        return {"success": local_result["success"], "output": f"🗑️ Branch '{branch}' deletado.\n" + "\n".join(outputs)}

    async def execute_operation(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        """Entry point: dispatch a git operation by name."""
        op = operation.lower().strip()
        repo_url = params.get("repo_url") or self.default_repo
        branch = params.get("branch") or self.default_branch
        message = params.get("message", "Kemy AI: atualização automática")
        files = params.get("files")
        create = params.get("create", False)
        remote_delete = params.get("remote", False)

        if not self.is_configured:
            return {
                "success": False,
                "output": "⚠️ Integração GitHub não configurada. Adicione GITHUB_TOKEN nas variáveis de ambiente.",
            }

        if op in ("clone",):
            return await self.clone(repo_url or "", branch)
        elif op in ("pull", "update", "atualizar"):
            return await self.pull(repo_url=repo_url, branch=branch)
        elif op in ("status", "estado"):
            return await self.status(repo_url=repo_url)
        elif op in ("commit", "commitar", "salvar"):
            return await self.commit(message=message, files=files, repo_url=repo_url)
        elif op in ("push", "enviar"):
            return await self.push(branch=branch, repo_url=repo_url)
        elif op in ("branches", "branch", "listar-branches"):
            return await self.list_branches(repo_url=repo_url)
        elif op in ("checkout", "mudar-branch"):
            return await self.checkout(branch=branch, create=create, repo_url=repo_url)
        elif op in ("delete-branch", "deletar-branch", "remover-branch"):
            return await self.delete_branch(branch=branch, remote=remote_delete, repo_url=repo_url)
        else:
            return {"success": False, "output": f"Operação '{operation}' não reconhecida."}
