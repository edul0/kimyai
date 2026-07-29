from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .artifact_parser import ParsedArtifact, parse_kemy_artifact
from .agent_runtime import ImpactGraph, LocalSemanticSearch


IGNORED_DIRS = {
    ".git", ".venv", "venv", ".build-venv", "node_modules", "dist", "build",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "generated_documents",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss", ".json",
    ".toml", ".yaml", ".yml", ".md", ".sql", ".env.example", ".ps1", ".sh",
}
IMPORTANT_FILES = {
    "pyproject.toml", "requirements.txt", "requirements-cloud.txt", "package.json",
    "tsconfig.json", "vite.config.ts", "dockerfile", "readme.md", ".env.example",
}
PLACEHOLDERS = ("TODO", "FIXME", "adicione aqui", "restante do codigo", "restante do código")
COLOR_TERMS = {
    "azul": ("blue", "#0", "rgb("), "verde": ("green", "#0", "rgb("),
    "vermelho": ("red", "#f", "rgb("), "preto": ("black", "#000", "#111", "#0a0a"),
    "branco": ("white", "#fff", "#f8", "#faf"), "roxo": ("purple", "violet", "#7", "#8"),
    "laranja": ("orange", "#f5", "#ff8"), "rosa": ("pink", "#ec", "#f4"),
}
FEATURE_TERMS = {
    "sidebar": ("sidebar", "aside"), "menu lateral": ("sidebar", "aside"),
    "navbar": ("navbar", "<nav"), "busca": ("search", "buscar", "pesquisar"),
    "filtro": ("filter", "filtro"), "modal": ("modal", "dialog"),
    "formulario": ("<form", "formulario", "form-group"), "formulário": ("<form", "formulario", "form-group"),
    "grafico": ("chart", "canvas", "recharts", "grafico"), "gráfico": ("chart", "canvas", "recharts", "grafico"),
    "responsivo": ("@media", "grid", "flex"), "mobile": ("@media", "viewport"),
}


@dataclass
class RepositoryContext:
    root: str
    tree: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    symbols: list[dict[str, Any]] = field(default_factory=list)
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    related_tests: list[str] = field(default_factory=list)
    impact_graph: dict[str, Any] = field(default_factory=dict)

    def as_prompt(self) -> str:
        excerpts = []
        for item in self.files:
            excerpts.append(f"### {item['path']}\n```{item['language']}\n{item['content']}\n```")
        return (
            f"Raiz analisada: {self.root}\n"
            f"Stack detectada: {', '.join(self.stack) or 'nao detectada'}\n"
            f"Comandos seguros: {', '.join(self.commands) or 'nenhum detectado'}\n"
            f"Git: {json.dumps(self.git, ensure_ascii=False)}\n"
            f"Simbolos relacionados: {json.dumps(self.symbols[:80], ensure_ascii=False)}\n"
            f"Dependencias/imports: {json.dumps(self.dependencies, ensure_ascii=False)[:7000]}\n"
            f"Testes relacionados: {', '.join(self.related_tests) or 'nenhum localizado'}\n"
            f"Grafo de impacto: {json.dumps(self.impact_graph, ensure_ascii=False)[:7000]}\n"
            f"Arvore relevante ({len(self.tree)} arquivos):\n- " + "\n- ".join(self.tree) +
            ("\n\nArquivos mais relacionados ao pedido:\n" + "\n\n".join(excerpts) if excerpts else "")
        )

    def technical_memory(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "stack": self.stack,
            "commands": self.commands,
            "important_files": self.tree[:40],
            "symbols": self.symbols[:80],
            "dependencies": self.dependencies,
            "git": self.git,
            "related_tests": self.related_tests,
            "impact_graph": self.impact_graph,
        }


@dataclass
class ValidationReport:
    ok: bool
    checks: list[dict[str, Any]]
    errors: list[str]
    warnings: list[str]

    def as_prompt(self) -> str:
        lines = [f"Status: {'APROVADO' if self.ok else 'REPROVADO'}"]
        lines.extend(f"- ERRO: {item}" for item in self.errors)
        lines.extend(f"- AVISO: {item}" for item in self.warnings)
        for check in self.checks:
            lines.append(f"- {check['name']}: {check['status']}{(' - ' + check['detail']) if check.get('detail') else ''}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": self.checks, "errors": self.errors, "warnings": self.warnings}


class FrontendInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.assets: list[str] = []
        self.inline_scripts: list[str] = []
        self.buttons: list[dict[str, str]] = []
        self.visible_text: list[str] = []
        self._in_script = False
        self._script_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "img", "link", "source"}:
            asset = values.get("src") or values.get("href")
            if asset:
                self.assets.append(asset)
        if tag == "script" and not values.get("src"):
            self._in_script = True
            self._script_chunks = []
        if tag == "button" or values.get("role") == "button":
            self.buttons.append({
                "id": values.get("id", ""),
                "onclick": values.get("onclick", ""),
                "type": values.get("type", ""),
            })

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_script:
            script = "".join(self._script_chunks).strip()
            if script:
                self.inline_scripts.append(script)
            self._in_script = False
            self._script_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._script_chunks.append(data)
        elif data.strip():
            self.visible_text.append(data.strip())


class RepositoryIntelligence:
    def __init__(self, root: str | Path, max_files: int = 180, max_context_chars: int = 28000):
        self.root = Path(root).resolve()
        self.max_files = max_files
        self.max_context_chars = max_context_chars

    def build_context(self, request: str) -> RepositoryContext:
        paths = self._walk()
        tokens = set(re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]{2,}", request.lower()))
        ranked = sorted(paths, key=lambda path: self._rank(path, tokens))
        budget = self.max_context_chars
        selected: list[dict[str, str]] = []
        for path in ranked[:14]:
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            clipped = content[: min(7000, budget)]
            if not clipped:
                continue
            selected.append({
                "path": path.relative_to(self.root).as_posix(),
                "language": self._language(path),
                "content": clipped,
            })
            budget -= len(clipped)
            if budget < 800:
                break
        relative = [path.relative_to(self.root).as_posix() for path in paths]
        dependencies = self._extract_dependencies(ranked[:50])
        symbols = self._extract_symbols(ranked[:40], tokens)
        semantic_candidates = [
            {"path": item["path"], "content": item["content"], "language": item["language"]}
            for item in selected
        ]
        semantic_ranked = LocalSemanticSearch().rank(request, semantic_candidates, limit=14)
        if semantic_ranked:
            selected = semantic_ranked
        selected_paths = [item["path"] for item in selected]
        return RepositoryContext(
            root=str(self.root),
            tree=relative,
            files=selected,
            stack=self._detect_stack(relative),
            commands=self._detect_commands(relative),
            symbols=symbols,
            dependencies=dependencies,
            git=self._git_context(),
            related_tests=self._related_tests(relative, selected_paths, tokens),
            impact_graph=ImpactGraph().build(dependencies, symbols),
        )

    def _walk(self) -> list[Path]:
        files: list[Path] = []
        if not self.root.exists() or not self.root.is_dir():
            return files
        for current, dirs, names in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith(".cache"))
            for name in sorted(names):
                path = Path(current) / name
                suffix = path.suffix.lower()
                if suffix in TEXT_SUFFIXES or name.lower() in IMPORTANT_FILES:
                    try:
                        if path.stat().st_size <= 350_000:
                            files.append(path)
                    except OSError:
                        continue
                if len(files) >= self.max_files:
                    return files
        return files

    def _rank(self, path: Path, tokens: set[str]) -> tuple[int, int, str]:
        rel = path.relative_to(self.root).as_posix().lower()
        name = path.name.lower()
        matches = sum(1 for token in tokens if token in rel)
        important = 1 if name in IMPORTANT_FILES else 0
        source = 1 if any(part in rel for part in ("app/", "src/", "tests/", "local_app/")) else 0
        return (-(matches * 5 + important * 3 + source), len(rel), rel)

    def _extract_symbols(self, paths: list[Path], request_tokens: set[str]) -> list[dict[str, Any]]:
        symbols: list[dict[str, Any]] = []
        for path in paths:
            if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                rel = path.relative_to(self.root).as_posix()
                pattern = re.compile(
                    r"(?:export\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)"
                    r"|(?:export\s+)?(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("
                )
                for match in pattern.finditer(content):
                    name = match.group(1) or match.group(2)
                    line = content.count("\n", 0, match.start()) + 1
                    score = sum(token in name.lower() or name.lower() in token for token in request_tokens)
                    symbols.append({
                        "path": rel, "name": name,
                        "kind": "class" if "class" in match.group(0) else "function",
                        "line": line, "args": [], "_score": score,
                    })
                continue
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, SyntaxError):
                continue
            rel = path.relative_to(self.root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    args = []
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        args = [item.arg for item in node.args.args]
                    score = sum(token in node.name.lower() or node.name.lower() in token for token in request_tokens)
                    symbols.append({
                        "path": rel, "name": node.name, "kind": kind,
                        "line": getattr(node, "lineno", 0), "args": args, "_score": score,
                    })
        symbols.sort(key=lambda item: (-item.pop("_score"), item["path"], item["line"]))
        return symbols[:160]

    def _extract_dependencies(self, paths: list[Path]) -> dict[str, list[str]]:
        dependencies: dict[str, list[str]] = {}
        for path in paths:
            if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                imports = re.findall(
                    r"(?:import[\s\S]*?\sfrom\s|require\s*\()\s*[\"']([^\"']+)[\"']",
                    content,
                )
                if imports:
                    dependencies[path.relative_to(self.root).as_posix()] = sorted(set(imports))[:30]
                continue
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, SyntaxError):
                continue
            imports: list[str] = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    module = ("." * node.level) + (node.module or "")
                    imports.append(module)
            if imports:
                dependencies[path.relative_to(self.root).as_posix()] = sorted(set(imports))[:30]
            if len(dependencies) >= 40:
                break
        return dependencies

    @staticmethod
    def _related_tests(paths: list[str], selected_paths: list[str], tokens: set[str]) -> list[str]:
        candidates = [
            path for path in paths
            if path.startswith(("tests/", "test/", "__tests__/"))
            or "/__tests__/" in path
            or Path(path).name.startswith("test_")
            or Path(path).name.endswith((".test.js", ".test.ts", ".test.tsx", ".spec.js", ".spec.ts", ".spec.tsx"))
        ]
        source_stems = {Path(path).stem.replace("test_", "").replace(".test", "").replace(".spec", "") for path in selected_paths}
        ranked = sorted(
            candidates,
            key=lambda path: (
                -sum(stem and stem in path.lower() for stem in source_stems),
                -sum(token in path.lower() for token in tokens),
                path,
            ),
        )
        return ranked[:20]

    def _git_context(self) -> dict[str, Any]:
        git = shutil.which("git")
        if not git or not (self.root / ".git").exists():
            return {"available": False}
        safe_root = str(self.root).replace("\\", "/")
        base = [git, "-c", f"safe.directory={safe_root}", "-C", str(self.root)]
        try:
            branch = subprocess.run(
                base + ["branch", "--show-current"], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            status = subprocess.run(
                base + ["status", "--short"], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.splitlines()
            recent = subprocess.run(
                base + ["log", "-5", "--pretty=%h %s"], capture_output=True, text=True, timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.splitlines()
            return {"available": True, "branch": branch, "dirty": status[:30], "recent_commits": recent}
        except (OSError, subprocess.SubprocessError):
            return {"available": False}

    @staticmethod
    def _detect_stack(paths: list[str]) -> list[str]:
        joined = "\n".join(paths).lower()
        stack = []
        signals = (
            ("requirements", "Python"),
            ("app/main.py", "FastAPI"),
            ("package.json", "Node.js"),
            (".tsx", "React/TypeScript"),
            ("supabase/", "Supabase"),
            ("dockerfile", "Docker"),
        )
        for marker, label in signals:
            if marker in joined and label not in stack:
                stack.append(label)
        return stack

    @staticmethod
    def _detect_commands(paths: list[str]) -> list[str]:
        lowered = {item.lower() for item in paths}
        commands = []
        if any(item.startswith("tests/") and item.endswith(".py") for item in lowered):
            commands.append("python -m pytest -q")
        if any(item.endswith(".py") for item in lowered):
            commands.append("python -m compileall -q app")
        if "package.json" in lowered:
            commands.extend(["npm test -- --run", "npm run build"])
        return commands

    @staticmethod
    def _language(path: Path) -> str:
        return {
            ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript",
            ".tsx": "tsx", ".html": "html", ".css": "css", ".json": "json",
            ".md": "markdown", ".sql": "sql", ".yml": "yaml", ".yaml": "yaml",
        }.get(path.suffix.lower(), "text")


class SafeCodeValidator:
    """Validates generated files in an isolated temp directory; never executes generated programs."""

    def validate(
        self,
        raw: str,
        existing_paths: set[str] | None = None,
        allow_new_files: bool = True,
        base_root: str | Path | None = None,
        require_tests: bool = False,
        design_request: str = "",
    ) -> ValidationReport:
        artifact = parse_kemy_artifact(raw)
        if not artifact:
            return ValidationReport(
                ok=False, checks=[], errors=["Resposta de coding nao trouxe kemy_artifact com arquivos verificaveis."], warnings=[]
            )
        return self.validate_artifact(
            artifact,
            existing_paths=existing_paths,
            allow_new_files=allow_new_files,
            base_root=base_root,
            require_tests=require_tests,
            design_request=design_request,
        )

    def validate_artifact(
        self,
        artifact: ParsedArtifact,
        existing_paths: set[str] | None = None,
        allow_new_files: bool = True,
        base_root: str | Path | None = None,
        require_tests: bool = False,
        design_request: str = "",
    ) -> ValidationReport:
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        normalized_artifact_paths = [item.path.replace("\\", "/").lstrip("/") for item in artifact.files]
        source_paths = [
            path for path in normalized_artifact_paths
            if path.endswith((".py", ".js", ".jsx", ".ts", ".tsx"))
            and not self._is_test_path(path)
        ]
        if require_tests and source_paths and not any(self._is_test_path(path) for path in normalized_artifact_paths):
            errors.append("Alteracao de codigo sem teste relacionado no artifact.")
        if design_request:
            design_checks, design_errors, design_warnings = self._validate_design_alignment(artifact, design_request)
            checks.extend(design_checks)
            errors.extend(design_errors)
            warnings.extend(design_warnings)
        with tempfile.TemporaryDirectory(prefix="kemy-code-qa-") as temp:
            root = Path(temp).resolve()
            source_root = Path(base_root).resolve() if base_root else None
            if source_root and source_root.is_dir():
                for relative in sorted(existing_paths or set()):
                    source = (source_root / relative).resolve()
                    if source_root not in source.parents or not source.is_file():
                        continue
                    destination = root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(source, destination)
                    except OSError:
                        continue
            written: list[Path] = []
            for item in artifact.files:
                normalized_path = item.path.replace("\\", "/").lstrip("/")
                target = (root / normalized_path).resolve()
                if root not in target.parents:
                    errors.append(f"Caminho inseguro rejeitado: {item.path}")
                    continue
                is_test_or_migration = normalized_path.startswith(("tests/", "supabase/migrations/"))
                if existing_paths is not None and normalized_path not in existing_paths and not allow_new_files and not is_test_or_migration:
                    errors.append(
                        f"Arquivo inexistente no repositorio: {normalized_path}. "
                        "Edite um caminho real ou declare explicitamente que este arquivo precisa ser criado."
                    )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item.content, encoding="utf-8")
                written.append(target)
                found = [marker for marker in PLACEHOLDERS if marker.lower() in item.content.lower()]
                if found:
                    warnings.append(f"{item.path}: placeholder encontrado ({found[0]}).")

            for path in written:
                rel = path.relative_to(root).as_posix()
                try:
                    if path.suffix == ".py":
                        ast.parse(path.read_text(encoding="utf-8"), filename=rel)
                        checks.append({"name": f"python-ast:{rel}", "status": "ok"})
                    elif path.suffix == ".json" or path.name == "package.json":
                        json.loads(path.read_text(encoding="utf-8"))
                        checks.append({"name": f"json:{rel}", "status": "ok"})
                except (SyntaxError, json.JSONDecodeError) as exc:
                    errors.append(f"{rel}: {type(exc).__name__}: {exc}")

            python_files = [path for path in written if path.suffix == ".py"]
            if python_files:
                proc = subprocess.run(
                    [os.sys.executable, "-m", "compileall", "-q", str(root)],
                    capture_output=True, text=True, timeout=20, cwd=root,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                detail = (proc.stderr or proc.stdout).strip()[:1200]
                checks.append({"name": "python-compileall", "status": "ok" if proc.returncode == 0 else "failed", "detail": detail})
                if proc.returncode:
                    errors.append(f"compileall falhou: {detail}")
                ruff = shutil.which("ruff")
                if ruff:
                    proc = subprocess.run(
                        [ruff, "check", "--no-cache", str(root)],
                        capture_output=True, text=True, timeout=25, cwd=root,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    detail = (proc.stdout or proc.stderr).strip()[:1600]
                    checks.append({"name": "ruff", "status": "ok" if proc.returncode == 0 else "failed", "detail": detail})
                    if proc.returncode:
                        errors.append(f"Ruff encontrou problemas: {detail}")

            node = shutil.which("node")
            for path in [item for item in written if item.suffix == ".js"][:8]:
                if not node:
                    warnings.append("Node.js nao encontrado; validacao sintatica JS ignorada.")
                    break
                proc = subprocess.run(
                    [node, "--check", str(path)], capture_output=True, text=True, timeout=15,
                    cwd=root, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                detail = (proc.stderr or proc.stdout).strip()[:1200]
                checks.append({"name": f"node-check:{path.name}", "status": "ok" if proc.returncode == 0 else "failed", "detail": detail})
                if proc.returncode:
                    errors.append(f"{path.name}: node --check falhou: {detail}")

            for html_path in [item for item in written if item.suffix.lower() == ".html"]:
                rel = html_path.relative_to(root).as_posix()
                inspector = FrontendInspector()
                try:
                    inspector.feed(html_path.read_text(encoding="utf-8", errors="ignore"))
                except Exception as exc:
                    errors.append(f"{rel}: HTML invalido ({type(exc).__name__}: {exc})")
                    continue
                visible = " ".join(inspector.visible_text).strip()
                checks.append({
                    "name": f"html-content:{rel}",
                    "status": "ok" if visible else "failed",
                    "detail": f"{len(visible)} caracteres visiveis",
                })
                if not visible:
                    errors.append(f"{rel}: pagina sem conteudo visivel; risco de tela vazia/preta.")
                for asset in inspector.assets:
                    clean = asset.split("?", 1)[0].split("#", 1)[0]
                    if not clean or re.match(r"^(?:https?:|data:|blob:|//|#)", clean, flags=re.I):
                        continue
                    asset_path = (html_path.parent / clean.lstrip("/")).resolve()
                    if root not in asset_path.parents or not asset_path.exists():
                        errors.append(f"{rel}: asset local inexistente: {asset}")
                combined_scripts = "\n".join(inspector.inline_scripts)
                for button in inspector.buttons:
                    button_id = button.get("id")
                    has_handler = bool(button.get("onclick"))
                    if button_id:
                        has_handler = has_handler or bool(re.search(
                            rf"(?:getElementById\s*\(\s*[\"']{re.escape(button_id)}[\"']|#{re.escape(button_id)}\b)",
                            combined_scripts,
                        ))
                    if not has_handler and button.get("type") != "submit":
                        warnings.append(
                            f"{rel}: botao sem handler detectavel"
                            + (f" (#{button_id})" if button_id else "") + "."
                        )
                if node:
                    for index, script in enumerate(inspector.inline_scripts[:8], start=1):
                        script_path = root / f"__kemy_inline_{index}.js"
                        script_path.write_text(script, encoding="utf-8")
                        proc = subprocess.run(
                            [node, "--check", str(script_path)], capture_output=True, text=True,
                            timeout=15, cwd=root,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                        detail = (proc.stderr or proc.stdout).strip()[:1200]
                        checks.append({
                            "name": f"inline-js:{rel}:{index}",
                            "status": "ok" if proc.returncode == 0 else "failed",
                            "detail": detail,
                        })
                        if proc.returncode:
                            errors.append(f"{rel}: JavaScript inline invalido: {detail}")
        return ValidationReport(ok=not errors, checks=checks, errors=errors, warnings=warnings)

    @staticmethod
    def _is_test_path(path: str) -> bool:
        name = Path(path).name.lower()
        return (
            path.startswith(("tests/", "test/", "__tests__/"))
            or "/__tests__/" in path
            or name.startswith("test_")
            or ".test." in name
            or ".spec." in name
        )

    @staticmethod
    def _validate_design_alignment(
        artifact: ParsedArtifact, request: str
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        request_l = request.lower()
        content = "\n".join(item.content for item in artifact.files).lower()
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []

        explicit_hex = sorted(set(re.findall(r"#[0-9a-fA-F]{3,8}\b", request)))
        for color in explicit_hex:
            found = color.lower() in content
            checks.append({"name": f"design-color:{color}", "status": "ok" if found else "failed"})
            if not found:
                errors.append(f"Design nao respeitou a cor explicitamente pedida: {color}.")

        for term, signals in COLOR_TERMS.items():
            if term not in request_l:
                continue
            found = any(signal in content for signal in signals)
            checks.append({"name": f"design-color:{term}", "status": "ok" if found else "warning"})
            if not found:
                warnings.append(f"Cor/tema `{term}` pedido nao foi identificado no codigo visual.")

        for feature, signals in FEATURE_TERMS.items():
            if feature not in request_l:
                continue
            found = any(signal in content for signal in signals)
            checks.append({"name": f"design-feature:{feature}", "status": "ok" if found else "failed"})
            if not found:
                errors.append(f"Funcionalidade/componente visual pedido nao encontrado: {feature}.")

        name_patterns = (
            r"(?:chamad[oa]|nome\s+(?:do\s+)?(?:site|app|sistema|marca)\s*(?:é|e|:)?|marca\s+)"
            r"\s*[\"'“”]?([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 _-]{1,40}?)"
            r"[\"'“”]?(?=\s+(?:com|que|e|para|usando)\b|[,.;]|$)"
        )
        match = re.search(name_patterns, request, flags=re.I)
        if match:
            requested_name = match.group(1).strip(" \"'“”.,;:")
            requested_name = re.split(r"\s+(?:com|que|e|para|usando)\s+", requested_name, maxsplit=1, flags=re.I)[0]
            if requested_name and requested_name.lower() not in content:
                errors.append(f"Nome/marca explicitamente pedido nao aparece na interface: {requested_name}.")
                checks.append({"name": "design-brand-name", "status": "failed", "detail": requested_name})
            elif requested_name:
                checks.append({"name": "design-brand-name", "status": "ok", "detail": requested_name})

        anti_generic = ("lorem ipsum", "empresa xyz", "seu logo", "produto 1", "item 1")
        generic_found = [marker for marker in anti_generic if marker in content]
        if generic_found:
            errors.append(f"Design generico/placeholder encontrado: {generic_found[0]}.")
        checks.append({
            "name": "design-request-alignment",
            "status": "ok" if not errors else "failed",
            "detail": f"{len(checks)} requisitos visuais verificados",
        })
        return checks, errors, warnings


def reasoning_budget(request: str, repository_context: RepositoryContext | None = None) -> int:
    """Returns 1-3 review passes according to task risk while preserving free-tier quotas."""
    text = request.lower()
    score = 0
    score += min(len(request) // 700, 2)
    score += sum(marker in text for marker in ("refator", "seguran", "auth", "banco", "migra", "concorr", "arquitet"))
    score += sum(marker in text for marker in ("corrija", "bug", "erro", "teste", "deploy"))
    if repository_context and len(repository_context.files) >= 8:
        score += 1
    return 3 if score >= 5 else 2 if score >= 2 else 1
