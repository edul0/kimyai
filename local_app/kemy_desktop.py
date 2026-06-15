"""Kemy Desktop Local - assistente de voz com sistema de conversas.

Executavel que transforma a Kemy num companheiro local que:
  - sobe o backend FastAPI no seu PC e usa SUAS chaves de IA (via .env);
  - organiza o trabalho em CONVERSAS: cada conversa tem sua sessao (contexto) e
    o seu proprio projeto/pasta; voltar numa conversa continua o mesmo site;
  - ouve voce (STT) e responde falando com a boca em tempo real (TTS);
  - mostra um avatar (imagem anime opcional, para futura conexao com VTuber);
  - salva o codigo gerado na pasta da conversa e abre o preview;
  - pode rodar comandos no PC (com ou sem confirmacao) e se auto-atualizar.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import http.cookiejar
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog


if getattr(sys, "frozen", False):
    ROOT_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    EXE_DIR = Path(sys.executable).resolve().parent
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]
    EXE_DIR = ROOT_DIR

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

APP_USER = os.environ.get("KEMY_AUTH_USER", "admin")
APP_PASSWORD = os.environ.get("KEMY_AUTH_PASSWORD", "kemy-ai")
APP_SECRET = os.environ.get("KEMY_AUTH_SECRET", "kemy-local-desktop-secret")

RELEASES_URL = "https://github.com/edul0/kimyai/releases"
RELEASE_API = "https://api.github.com/repos/edul0/kimyai/releases/tags/desktop-latest"

# Render: backend hospedado que JA tem as chaves de IA (LLM_MODE=providers).
# Usado quando nao ha .env local, para nao precisar configurar chave toda vez.
ONLINE_URL = "https://kemy-ai.onrender.com"

CAPABILITY_NOTE = (
    "\n\n[Sistema: voce roda no PC Windows do usuario por um app local com acesso "
    "ao sistema. SE E SOMENTE SE o pedido exigir executar, abrir ou instalar algo "
    "no PC, inclua os comandos (Windows, um por linha) num bloco ```kemy-run. "
    "Para gerar codigo ou arquivos, responda normalmente como sempre.]"
)
UPDATE_NOTE = (
    "\n[Esta conversa ja tem um projeto em andamento. Atualize/edite os MESMOS "
    "arquivos do projeto atual; nao comece um site/projeto novo a menos que eu "
    "peca. Reenvie completos os arquivos que alterar.]"
)

COLORS = {
    "bg": "#0a0e15",
    "panel": "#121a26",
    "panel_soft": "#0e1520",
    "sidebar": "#0c121b",
    "line": "#1f2c3d",
    "text": "#e8eefc",
    "muted": "#8194b0",
    "accent": "#5ee0c0",
    "idle": "#5ee0c0",
    "listening": "#5ab0ff",
    "thinking": "#ffc857",
    "speaking": "#a78bfa",
    "offline": "#6b7b96",
    "error": "#f87171",
    "user": "#9fe7d2",
    "skin": "#ffe2d2",
    "hair": "#8b6fe6",
    "hair_dark": "#6f56c4",
    "eye_white": "#ffffff",
    "iris": "#3aa0b8",
    "blush": "#ff9bb0",
    "mouth": "#b8455e",
}

STATE_LABELS = {
    "idle": "● Pronta", "listening": "● Ouvindo...", "thinking": "● Pensando...",
    "speaking": "● Falando...", "offline": "● Conectando...", "error": "● Erro",
}


# --------------------------------------------------------------------------- #
# Config / arquivos locais
# --------------------------------------------------------------------------- #
def config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        d = Path(base) / "Kemy"
    else:
        d = Path.home() / ".config" / "kemy"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def find_env_file() -> Path | None:
    for cand in (EXE_DIR / ".env", config_dir() / ".env", ROOT_DIR / ".env",
                 ROOT_DIR / "kemy_bundled.env", Path.cwd() / ".env"):
        try:
            if cand.is_file() and cand.read_text(encoding="utf-8", errors="ignore").strip():
                return cand
        except Exception:
            continue
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and not key.startswith("#"):
            data[key] = value
    return data


def _detect_voice_support() -> dict[str, bool]:
    support = {"tts": False, "stt": False}
    try:
        import pyttsx3  # noqa: F401

        support["tts"] = True
    except Exception:
        pass
    try:
        import speech_recognition  # noqa: F401

        support["stt"] = True
    except Exception:
        pass
    return support


VOICE_SUPPORT = _detect_voice_support()

LANG_EXT = {
    "html": "index.html", "css": "styles.css", "javascript": "script.js",
    "js": "script.js", "typescript": "app.ts", "ts": "app.ts",
    "tsx": "App.tsx", "jsx": "App.jsx", "python": "main.py", "py": "main.py",
    "json": "data.json", "sql": "schema.sql", "bash": "script.sh",
    "sh": "script.sh", "yaml": "config.yaml", "yml": "config.yaml",
    "md": "README.md", "markdown": "README.md",
}


def extract_run_commands(text: str) -> list[str]:
    cmds: list[str] = []
    if not text:
        return cmds
    for match in re.finditer(r"```(?:kemy-run|run)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE):
        for line in match.group(1).splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                cmds.append(line)
    return cmds


def extract_code_files(text: str) -> list[dict]:
    files: list[dict] = []
    if not text:
        return files
    used: dict[str, int] = {}
    idx = 0
    for match in re.finditer(r"```([a-zA-Z0-9_+\-]*)[ \t]*\n(.*?)```", text, re.DOTALL):
        lang = (match.group(1) or "").lower().strip()
        if lang in ("kemy-run", "run"):
            continue
        code = match.group(2)
        if not code.strip():
            continue
        idx += 1
        name = LANG_EXT.get(lang, f"arquivo{idx}.txt")
        if name in used:
            used[name] += 1
            stem, _, ext = name.rpartition(".")
            name = f"{stem}_{used[name]}.{ext}" if ext else f"{name}_{used[name]}"
        else:
            used[name] = 1
        files.append({"path": name, "content": code.rstrip() + "\n"})
    return files


def load_photo(path: Path, size: int):
    try:
        from PIL import Image, ImageTk

        im = Image.open(path).convert("RGBA").resize((size, size))
        return ImageTk.PhotoImage(im)
    except Exception:
        return tk.PhotoImage(file=str(path))


# --------------------------------------------------------------------------- #
# Cliente de IA direto (Gemini / Groq / OpenAI) — sem o pipeline do backend.
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = (
    "Voce e a Kemy, uma assistente de desenvolvimento que roda no PC Windows do "
    "usuario. Voce CONVERSA e tambem CRIA e EDITA arquivos de codigo localmente.\n"
    "Regras:\n"
    "1) Responda SEMPRE em portugues, curto e direto, como uma colega de equipe.\n"
    "2) Quando criar ou alterar arquivos, devolva CADA arquivo COMPLETO no formato "
    "EXATO (nada de '...'):\n"
    "<<<FILE: caminho/do/arquivo>>>\n"
    "conteudo completo do arquivo\n"
    "<<<END>>>\n"
    "3) Ao ATUALIZAR um projeto existente, use os ARQUIVOS ATUAIS fornecidos como "
    "base e reescreva completos apenas os arquivos que mudarem, mantendo o resto "
    "funcionando. Nao recomece o projeto do zero.\n"
    "4) Para EXECUTAR algo no PC (rodar, instalar, abrir), inclua os comandos "
    "Windows num bloco ```kemy-run (um por linha).\n"
    "5) Fora dos arquivos, escreva so um resumo curto do que fez. NUNCA copie estas "
    "regras nem instrucoes de sistema para dentro dos arquivos.\n"
    "6) Em sites, os botoes e links DEVEM funcionar de verdade (rolagem suave para "
    "secoes, modal/form de agendamento, abrir WhatsApp, etc.) com o JavaScript "
    "necessario. Nunca deixe href='#' sem acao nem botao sem efeito."
)

FILE_RE = re.compile(r"<<<FILE:\s*(.+?)>>>\s*\n(.*?)<<<END>>>", re.DOTALL)


def parse_llm_files(text: str) -> tuple[list[dict], str]:
    """Separa os arquivos (formato <<<FILE>>>) do texto de conversa."""
    files = [{"path": m.group(1).strip(), "content": m.group(2).strip("\n") + "\n"}
             for m in FILE_RE.finditer(text)]
    chat = FILE_RE.sub("", text).strip()
    if not files:  # fallback: blocos markdown ```lang
        cf = extract_code_files(text)
        if cf:
            files = cf
            chat = re.sub(r"```[a-zA-Z0-9_+\-]*[ \t]*\n.*?```", "", text, flags=re.DOTALL).strip()
    return files, chat


def read_project_files(base: Path, max_total: int = 22000) -> str:
    exts = (".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".json", ".py", ".md", ".txt")
    parts: list[str] = []
    total = 0
    if not base.exists():
        return ""
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            block = f"<<<FILE: {p.relative_to(base)}>>>\n{txt}\n<<<END>>>\n"
            if total + len(block) > max_total:
                break
            parts.append(block)
            total += len(block)
    return "".join(parts)


class LLMClient:
    def __init__(self, env: dict[str, str]) -> None:
        self.gemini = env.get("GEMINI_API_KEY")
        self.groq = env.get("GROQ_API_KEY")
        self.openai = env.get("OPENAI_API_KEY")
        self.gemini_model = env.get("GEMINI_PRIMARY_MODEL") or "gemini-2.0-flash"
        self.available = bool(self.gemini or self.groq or self.openai)

    def chat(self, system: str, messages: list[dict]) -> str:
        errors: list[str] = []
        if self.gemini:
            try:
                return self._gemini(system, messages)
            except Exception as exc:
                errors.append(f"gemini: {exc}")
        if self.groq:
            try:
                return self._openai_compat(
                    "https://api.groq.com/openai/v1/chat/completions", self.groq,
                    "llama-3.3-70b-versatile", system, messages)
            except Exception as exc:
                errors.append(f"groq: {exc}")
        if self.openai:
            try:
                return self._openai_compat(
                    "https://api.openai.com/v1/chat/completions", self.openai,
                    "gpt-4o-mini", system, messages)
            except Exception as exc:
                errors.append(f"openai: {exc}")
        raise RuntimeError("; ".join(errors) or "Sem provedor de IA configurado.")

    def _post(self, url: str, headers: dict, payload: dict, timeout: float = 120) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _gemini(self, system: str, messages: list[dict]) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.gemini_model}:generateContent?key={self.gemini}")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages]
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": 8192},
        }
        data = self._post(url, {"Content-Type": "application/json"}, payload)
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def _openai_compat(self, url: str, key: str, model: str, system: str, messages: list[dict]) -> str:
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        payload = {"model": model, "messages": msgs, "temperature": 0.6}
        data = self._post(url, {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, payload)
        return data["choices"][0]["message"]["content"]


# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #
def run_server(host: str, port: int) -> None:
    os.chdir(ROOT_DIR)
    import uvicorn

    if getattr(sys, "frozen", False):
        from app.main import app as fastapi_app

        uvicorn.run(fastapi_app, host=host, port=port, log_level="info")
    else:
        uvicorn.run("agencia_kemy:app", host=host, port=port, reload=False, log_level="info")


def _healthcheck(url: str, timeout_seconds: float = 1.8) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


class LocalAPI:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session_id: str | None = None
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies)
        )

    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 90.0) -> dict:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._opener.open(req, timeout=timeout) as response:
            body = response.read().decode("utf-8") or "{}"
        return json.loads(body)

    def login(self, email: str, password: str) -> dict:
        return self._request("POST", "/api/auth/login", {"email": email, "password": password}, timeout=25)

    def new_session(self) -> str:
        data = self._request("POST", "/api/sessao/nova", {}, timeout=25)
        return data.get("session_id") or ""

    def send_command(self, message: str, session_id: str | None, modo: str = "coding") -> dict:
        payload = {"mensagem": message, "modo": modo, "session_id": session_id}
        return self._request("POST", "/api/comando", payload, timeout=40)

    def job_status(self, job_id: str) -> dict:
        return self._request("GET", f"/api/jobs/{job_id}", None, timeout=30)


def result_to_speech(resultado: dict | None, erro: str | None) -> tuple[str, str]:
    if erro:
        return ("Tive um problema ao executar o pedido.", f"Erro: {erro}")
    if not resultado:
        return ("Concluido.", "Concluido.")
    raw = str(resultado.get("raw") or "").strip()
    summary = str(resultado.get("summary") or "").strip()
    display = raw or summary or "Concluido."
    spoken = summary or raw or "Concluido."
    if len(spoken) > 600:
        spoken = spoken[:600].rsplit(" ", 1)[0] + "..."
    return (spoken, display)


# --------------------------------------------------------------------------- #
# Avatar
# --------------------------------------------------------------------------- #
class Avatar:
    def __init__(self, parent: tk.Widget, size: int = 200) -> None:
        self.size = size
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=COLORS["panel"],
                                highlightthickness=0)
        self.state = "idle"
        self.t = 0.0
        self.mouth_provider = None
        self.photo = None
        self.image_mode = False
        self._build()
        self.img_id = self.canvas.create_image(0, 0, state="hidden")
        self._animate()

    def set_image(self, photo) -> None:
        self.photo = photo
        self.image_mode = photo is not None
        self.canvas.itemconfig(self.img_id, image=photo if photo else "",
                               state="normal" if photo else "hidden")
        for item in self._vector_items:
            self.canvas.itemconfig(item, state="hidden" if self.image_mode else "normal")

    def _build(self) -> None:
        c = self.canvas
        self.glow = c.create_oval(0, 0, 0, 0, outline=COLORS["idle"], width=3)
        self.back_hair = c.create_polygon(0, 0, 0, 0, fill=COLORS["hair_dark"], smooth=True)
        self.ear_l = c.create_oval(0, 0, 0, 0, fill=COLORS["hair"], outline="")
        self.ear_r = c.create_oval(0, 0, 0, 0, fill=COLORS["hair"], outline="")
        self.cup_l = c.create_oval(0, 0, 0, 0, fill=COLORS["idle"], outline="")
        self.cup_r = c.create_oval(0, 0, 0, 0, fill=COLORS["idle"], outline="")
        self.head = c.create_oval(0, 0, 0, 0, fill=COLORS["skin"], outline="")
        self.band = c.create_arc(0, 0, 0, 0, style="arc", outline=COLORS["idle"], width=8)
        self.bangs = c.create_polygon(0, 0, 0, 0, fill=COLORS["hair"], smooth=True)
        self.tuft = c.create_polygon(0, 0, 0, 0, fill=COLORS["hair"], smooth=True)
        self.blush_l = c.create_oval(0, 0, 0, 0, fill=COLORS["blush"], outline="")
        self.blush_r = c.create_oval(0, 0, 0, 0, fill=COLORS["blush"], outline="")
        self.brow_l = c.create_line(0, 0, 0, 0, fill=COLORS["hair_dark"], width=4, capstyle="round")
        self.brow_r = c.create_line(0, 0, 0, 0, fill=COLORS["hair_dark"], width=4, capstyle="round")
        self.eye_l = c.create_oval(0, 0, 0, 0, fill=COLORS["eye_white"], outline="")
        self.eye_r = c.create_oval(0, 0, 0, 0, fill=COLORS["eye_white"], outline="")
        self.iris_l = c.create_oval(0, 0, 0, 0, fill=COLORS["iris"], outline="")
        self.iris_r = c.create_oval(0, 0, 0, 0, fill=COLORS["iris"], outline="")
        self.hi_l = c.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
        self.hi_r = c.create_oval(0, 0, 0, 0, fill="#ffffff", outline="")
        self.mouth = c.create_oval(0, 0, 0, 0, fill=COLORS["mouth"], outline="")
        self.dots = [c.create_oval(0, 0, 0, 0, fill=COLORS["thinking"], outline="") for _ in range(3)]
        self._vector_items = [
            self.back_hair, self.ear_l, self.ear_r, self.cup_l, self.cup_r, self.head,
            self.band, self.bangs, self.tuft, self.blush_l, self.blush_r, self.brow_l,
            self.brow_r, self.eye_l, self.eye_r, self.iris_l, self.iris_r, self.hi_l,
            self.hi_r, self.mouth, *self.dots,
        ]

    def set_state(self, state: str) -> None:
        self.state = state if state in COLORS else "idle"

    def _accent(self) -> str:
        return COLORS.get(self.state, COLORS["idle"])

    @staticmethod
    def _ov(c, item, x, y, rx, ry):
        c.coords(item, x - rx, y - ry, x + rx, y + ry)

    def _animate(self) -> None:
        self.t += 0.05
        c = self.canvas
        t = self.t
        accent = self._accent()
        S = self.size
        cx = S / 2
        sway = math.sin(t * 1.1) * (5 if self.state in ("idle", "speaking") else 2)
        bob = math.sin(t * 0.9) * 4
        cy = S * 0.52 + bob
        cx += sway
        rx, ry = S * 0.24, S * 0.26

        gr = rx + 30 + (6 * (0.5 + 0.5 * math.sin(t * 2)) if self.state in ("listening", "speaking") else 0)
        self._ov(c, self.glow, cx, cy, gr, gr)
        c.itemconfig(self.glow, outline=accent,
                     width=3 if self.state in ("listening", "speaking", "thinking") else 1)

        if self.image_mode:
            speak = 0.0
            if self.state == "speaking" and self.mouth_provider:
                try:
                    speak = float(self.mouth_provider())
                except Exception:
                    speak = 0.0
            self.canvas.coords(self.img_id, cx, cy + speak * 4)
            c.after(40, self._animate)
            return

        bw, bh = rx * 1.45, ry * 1.5
        c.coords(self.back_hair,
                 cx - bw, cy - bh * 0.5, cx - bw * 0.7, cy + bh, cx, cy + bh * 1.15,
                 cx + bw * 0.7, cy + bh, cx + bw, cy - bh * 0.5,
                 cx + bw * 0.4, cy - bh, cx - bw * 0.4, cy - bh)
        self._ov(c, self.head, cx, cy, rx, ry)
        ear_y = cy + ry * 0.1
        ex = rx * 1.02
        self._ov(c, self.ear_l, cx - ex, ear_y, S * 0.045, S * 0.055)
        self._ov(c, self.ear_r, cx + ex, ear_y, S * 0.045, S * 0.055)
        cup_pulse = (3 * (0.5 + 0.5 * math.sin(t * 4))) if self.state == "listening" else 0
        self._ov(c, self.cup_l, cx - ex, ear_y, S * 0.06 + cup_pulse, S * 0.075 + cup_pulse)
        self._ov(c, self.cup_r, cx + ex, ear_y, S * 0.06 + cup_pulse, S * 0.075 + cup_pulse)
        c.itemconfig(self.cup_l, fill=accent)
        c.itemconfig(self.cup_r, fill=accent)
        c.coords(self.band, cx - ex - S * 0.02, cy - ry - S * 0.05, cx + ex + S * 0.02, cy + ry * 0.2)
        c.itemconfig(self.band, outline=accent, start=10, extent=160)
        fy = cy - ry * 0.55
        c.coords(self.bangs,
                 cx - rx, cy - ry * 0.2, cx - rx * 0.95, fy - ry * 0.4, cx - rx * 0.3, cy - ry,
                 cx, fy, cx + rx * 0.3, cy - ry, cx + rx * 0.95, fy - ry * 0.4, cx + rx, cy - ry * 0.2,
                 cx + rx * 0.5, cy - ry * 0.35, cx, cy - ry * 0.15, cx - rx * 0.5, cy - ry * 0.35)
        c.coords(self.tuft, cx - 6, cy - ry * 0.98, cx + 2, cy - ry * 1.28, cx + 10, cy - ry * 0.98)
        blink = self._blink_factor(t)
        eye_y = cy + ry * 0.05
        eye_dx = rx * 0.46
        ew, eh = S * 0.052, S * 0.066 * blink
        self._ov(c, self.eye_l, cx - eye_dx, eye_y, ew, max(eh, 1))
        self._ov(c, self.eye_r, cx + eye_dx, eye_y, ew, max(eh, 1))
        look_y = -S * 0.018 if self.state == "thinking" else 0
        ir = S * 0.034 * (1 if blink > 0.4 else 0.2)
        self._ov(c, self.iris_l, cx - eye_dx, eye_y + look_y, ir, ir)
        self._ov(c, self.iris_r, cx + eye_dx, eye_y + look_y, ir, ir)
        c.itemconfig(self.iris_l, fill=accent if self.state != "idle" else COLORS["iris"])
        c.itemconfig(self.iris_r, fill=accent if self.state != "idle" else COLORS["iris"])
        hr = ir * 0.4
        self._ov(c, self.hi_l, cx - eye_dx - ir * 0.4, eye_y + look_y - ir * 0.4, hr, hr)
        self._ov(c, self.hi_r, cx + eye_dx - ir * 0.4, eye_y + look_y - ir * 0.4, hr, hr)
        vis = "normal" if blink > 0.4 else "hidden"
        for it in (self.iris_l, self.iris_r, self.hi_l, self.hi_r):
            c.itemconfig(it, state=vis)
        by = eye_y - eh - S * 0.03
        c.coords(self.brow_l, cx - eye_dx - ew, by + 2, cx - eye_dx + ew, by)
        c.coords(self.brow_r, cx + eye_dx - ew, by, cx + eye_dx + ew, by + 2)
        self._ov(c, self.blush_l, cx - eye_dx - S * 0.01, eye_y + S * 0.07, S * 0.03, S * 0.018)
        self._ov(c, self.blush_r, cx + eye_dx + S * 0.01, eye_y + S * 0.07, S * 0.03, S * 0.018)
        my = cy + ry * 0.5
        if self.state == "speaking":
            level = 0.5
            if self.mouth_provider:
                try:
                    level = float(self.mouth_provider())
                except Exception:
                    level = 0.5
            else:
                level = 0.5 + 0.5 * math.sin(t * 16)
            flutter = (math.sin(t * 24) * S * 0.004) if level > 0.3 else 0
            open_amt = level * S * 0.04 + S * 0.006 + flutter
            mw = S * 0.028 + level * S * 0.006
        else:
            open_amt = S * 0.006
            mw = S * 0.028
        self._ov(c, self.mouth, cx, my, mw, max(open_amt, S * 0.005))
        show = self.state == "thinking"
        for i, d in enumerate(self.dots):
            if not show:
                c.coords(d, 0, 0, 0, 0)
                continue
            dx = cx + rx * 0.9 + i * 11
            dy = cy - ry * 0.9 - 6 * math.sin(t * 4 + i)
            self._ov(c, d, dx, dy, 4, 4)
        c.after(40, self._animate)

    def _blink_factor(self, t: float) -> float:
        cycle = t % 3.2
        if cycle < 0.14:
            return max(0.05, abs(math.cos(cycle / 0.14 * math.pi)))
        return 1.0


# --------------------------------------------------------------------------- #
# Voz
# --------------------------------------------------------------------------- #
class Speaker:
    def __init__(self) -> None:
        self.available = VOICE_SUPPORT["tts"]
        self.on_start = None
        self.on_done = None
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._engine = None
        self._speaking = False
        self._last_word = 0.0
        if self.available:
            threading.Thread(target=self._loop, daemon=True).start()

    def _on_word(self, *_a, **_k) -> None:
        self._last_word = time.time()

    def mouth_level(self) -> float:
        if not self._speaking:
            return 0.0
        dt = time.time() - self._last_word
        if dt < 0.13:
            return 1.0
        if dt < 0.26:
            return 0.55
        return 0.18

    def _loop(self) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            self._engine = engine
            try:
                engine.connect("started-word", self._on_word)
            except Exception:
                pass
            try:
                for voice in engine.getProperty("voices"):
                    blob = f"{voice.id} {getattr(voice, 'name', '')} {getattr(voice, 'languages', '')}".lower()
                    if "pt" in blob or "brazil" in blob or "portug" in blob:
                        engine.setProperty("voice", voice.id)
                        break
                engine.setProperty("rate", 190)
            except Exception:
                pass
        except Exception:
            self.available = False
            return
        while True:
            text = self._queue.get()
            if not text:
                continue
            self._speaking = True
            self._last_word = time.time()
            if self.on_start:
                self.on_start()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass
            self._speaking = False
            if self.on_done and self._queue.empty():
                self.on_done()

    def say(self, text: str) -> None:
        if self.available and text.strip():
            self._queue.put(text)

    def stop(self) -> None:
        if not self.available:
            return
        try:
            while not self._queue.empty():
                self._queue.get_nowait()
        except Exception:
            pass
        self._speaking = False
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                pass


class Listener:
    def __init__(self) -> None:
        self.available = VOICE_SUPPORT["stt"]
        self._recognizer = None
        self._mic_ok = False
        if self.available:
            try:
                import speech_recognition as sr

                self._recognizer = sr.Recognizer()
                self._recognizer.dynamic_energy_threshold = True
                sr.Microphone.list_microphone_names()
                self._mic_ok = True
            except Exception:
                self.available = False

    def listen_once(self, on_state, on_text, on_error) -> None:
        if not (self.available and self._mic_ok):
            on_error("Microfone ou SpeechRecognition indisponivel.")
            return

        def _worker() -> None:
            import speech_recognition as sr

            try:
                with sr.Microphone() as source:
                    on_state("listening")
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.4)
                    audio = self._recognizer.listen(source, timeout=8, phrase_time_limit=14)
                on_state("thinking")
                text = self._recognizer.recognize_google(audio, language="pt-BR")
                on_text(text)
            except sr.WaitTimeoutError:
                on_error("Nao ouvi nada. Tente de novo.")
            except sr.UnknownValueError:
                on_error("Nao entendi o audio. Fale um pouco mais claro.")
            except Exception as exc:  # pragma: no cover
                on_error(f"Falha no reconhecimento: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
class KemyVoiceApp:
    def __init__(self, root: tk.Tk, host: str, port: int) -> None:
        self.root = root
        self.host = host
        self.port = int(port)
        self.base_url = f"http://{self.host}:{self.port}"
        self.api = LocalAPI(self.base_url)
        self.process: subprocess.Popen | None = None
        self.connected = False
        self.busy = False
        self.continuous = False
        self._avatar_photo = None

        self.env_path = find_env_file()
        self.env_file_vars = parse_env_file(self.env_path) if self.env_path else {}
        self.workspace_root = self._resolve_workspace_root()
        self.llm = LLMClient(self.env_file_vars)
        # Config online (Render) persistida -> nao precisa de .env toda vez.
        self.online_cfg_file = config_dir() / "online.json"
        self.online_cfg = self._load_online_cfg()
        # direct = chaves locais; online = usa o Render (com as chaves no servidor).
        self.mode = "direct" if self.llm.available else "online"
        if self.mode == "online":
            self.api = LocalAPI(self.online_cfg.get("url") or ONLINE_URL)
        self.convos_file = config_dir() / "conversations.json"
        self.convos: list[dict] = []
        self.active_id: str | None = None
        self._load_convos()

        self.speaker = Speaker()
        self.speaker.on_start = lambda: self.root.after(0, lambda: self._set_state("speaking"))
        self.speaker.on_done = lambda: self.root.after(0, self._on_speech_done)
        self.listener = Listener()

        self._build_ui()
        self.avatar.mouth_provider = self.speaker.mouth_level
        self._autoload_avatar()
        self._render_convos()
        self._render_transcript()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_state("offline")
        threading.Thread(target=self._bootstrap, daemon=True).start()

    # ----- conversas (persistencia) ----- #
    def _resolve_workspace_root(self) -> Path:
        raw = self.env_file_vars.get("KEMY_LOCAL_WORKSPACE_ROOT", "").strip()
        if raw:
            try:
                return Path(raw)
            except Exception:
                pass
        return Path.home() / "KemyWorkspace"

    def _load_convos(self) -> None:
        try:
            data = json.loads(self.convos_file.read_text(encoding="utf-8"))
            self.convos = data.get("items", [])
            self.active_id = data.get("active")
        except Exception:
            self.convos = []
            self.active_id = None
        if not self.convos:
            self._add_convo(select=False)
        if not self.active_id or not self._current():
            self.active_id = self.convos[0]["id"]

    def _save_convos(self) -> None:
        try:
            self.convos_file.write_text(
                json.dumps({"active": self.active_id, "items": self.convos}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _add_convo(self, select: bool = True) -> dict:
        cid = uuid.uuid4().hex[:8]
        item = {
            "id": cid, "title": "Nova conversa", "session_id": None,
            "project": str(self.workspace_root / f"projeto-{cid}"),
            "log": [], "updated": time.time(),
        }
        self.convos.insert(0, item)
        if select:
            self.active_id = cid
        self._save_convos()
        return item

    def _current(self) -> dict | None:
        return next((c for c in self.convos if c["id"] == self.active_id), None)

    def _has_ai_keys(self) -> bool:
        keys = ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
        return any(self.env_file_vars.get(k) for k in keys)

    # ----- UI ----- #
    def _build_ui(self) -> None:
        self.root.title("Kemy - Assistente de Voz")
        self.root.geometry("960x700")
        self.root.minsize(840, 600)
        self.root.configure(bg=COLORS["bg"])

        # Top bar
        top = tk.Frame(self.root, bg=COLORS["panel"], height=56)
        top.pack(fill="x", side="top")
        tk.Label(top, text="  Kemy", font=("Segoe UI", 17, "bold"),
                 fg=COLORS["text"], bg=COLORS["panel"]).pack(side="left", pady=12)
        tk.Label(top, text="  assistente local", font=("Segoe UI", 10),
                 fg=COLORS["muted"], bg=COLORS["panel"]).pack(side="left")
        self.state_label = tk.Label(top, text=STATE_LABELS["offline"], font=("Segoe UI", 10, "bold"),
                                    fg=COLORS["muted"], bg=COLORS["panel"])
        self.state_label.pack(side="right", padx=16)
        for txt, cmd in (("⬆ Atualizar", self._check_update),
                         ("📁 Pasta", self._open_workspace),
                         ("⚙ IA (.env)", self._import_env)):
            tk.Button(top, text=txt, command=cmd, bg=COLORS["panel"], fg=COLORS["muted"],
                      font=("Segoe UI", 9), relief="flat", padx=8, activebackground=COLORS["panel_soft"],
                      activeforeground=COLORS["text"], cursor="hand2").pack(side="right", padx=4)

        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        # Sidebar de conversas
        sidebar = tk.Frame(body, bg=COLORS["sidebar"], width=220)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Button(sidebar, text="＋  Nova conversa", command=self._new_conversation,
                  bg=COLORS["accent"], fg=COLORS["bg"], font=("Segoe UI", 10, "bold"),
                  relief="flat", pady=9, cursor="hand2").pack(fill="x", padx=12, pady=(12, 8))
        tk.Label(sidebar, text="CONVERSAS", font=("Segoe UI", 8, "bold"),
                 fg=COLORS["muted"], bg=COLORS["sidebar"]).pack(anchor="w", padx=16, pady=(4, 2))
        self.convo_list = tk.Frame(sidebar, bg=COLORS["sidebar"])
        self.convo_list.pack(fill="both", expand=True, padx=8)

        # Main
        main = tk.Frame(body, bg=COLORS["bg"])
        main.pack(side="left", fill="both", expand=True)

        avatar_wrap = tk.Frame(main, bg=COLORS["bg"])
        avatar_wrap.pack(pady=(10, 4))
        self.avatar = Avatar(avatar_wrap, size=180)
        self.avatar.canvas.pack()

        self.transcript = scrolledtext.ScrolledText(
            main, wrap="word", font=("Segoe UI", 10),
            bg=COLORS["panel_soft"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", padx=16, pady=12, borderwidth=0,
        )
        self.transcript.pack(fill="both", expand=True, padx=16, pady=6)
        self.transcript.tag_config("user", foreground=COLORS["user"], font=("Segoe UI", 10, "bold"),
                                   background="#16202e", spacing1=8, spacing3=8,
                                   lmargin1=12, lmargin2=12, rmargin=12)
        self.transcript.tag_config("kemy", foreground=COLORS["text"], font=("Segoe UI", 10),
                                   spacing1=6, spacing3=10, lmargin1=12, lmargin2=12, rmargin=12)
        self.transcript.tag_config("sys", foreground=COLORS["muted"], font=("Segoe UI", 9, "italic"),
                                   spacing1=2, spacing3=6, lmargin1=12, lmargin2=12)
        self.transcript.configure(state="disabled")

        entry_row = tk.Frame(main, bg=COLORS["bg"])
        entry_row.pack(fill="x", padx=16, pady=(0, 6))
        self.text_var = tk.StringVar()
        self.entry = tk.Entry(entry_row, textvariable=self.text_var, font=("Segoe UI", 11),
                              bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"],
                              relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._submit_text())
        tk.Button(entry_row, text="Enviar", command=self._submit_text, bg=COLORS["accent"],
                  fg=COLORS["bg"], font=("Segoe UI", 10, "bold"), relief="flat",
                  activebackground=COLORS["speaking"], padx=16, pady=7, cursor="hand2").pack(side="left")

        controls = tk.Frame(main, bg=COLORS["bg"])
        controls.pack(fill="x", padx=16, pady=(2, 14))
        self.talk_btn = tk.Button(controls, text="🎙  Falar", command=self._on_talk,
                                  bg=COLORS["listening"], fg=COLORS["bg"], font=("Segoe UI", 11, "bold"),
                                  relief="flat", activebackground=COLORS["speaking"], padx=20, pady=10,
                                  cursor="hand2")
        self.talk_btn.pack(side="left")
        self.continuous_var = tk.BooleanVar(value=False)
        tk.Checkbutton(controls, text="Modo conversa", variable=self.continuous_var,
                       command=self._toggle_continuous, bg=COLORS["bg"], fg=COLORS["muted"],
                       selectcolor=COLORS["panel"], activebackground=COLORS["bg"],
                       activeforeground=COLORS["text"], font=("Segoe UI", 9)).pack(side="left", padx=(12, 4))
        self.autonomous_var = tk.BooleanVar(value=False)
        tk.Checkbutton(controls, text="Rodar comandos sem confirmar", variable=self.autonomous_var,
                       bg=COLORS["bg"], fg=COLORS["muted"], selectcolor=COLORS["panel"],
                       activebackground=COLORS["bg"], activeforeground=COLORS["text"],
                       font=("Segoe UI", 9)).pack(side="left", padx=4)
        tk.Button(controls, text="Silenciar", command=self.speaker.stop, bg=COLORS["panel"],
                  fg=COLORS["text"], font=("Segoe UI", 10), relief="flat", padx=14, pady=8,
                  cursor="hand2").pack(side="right")

        if not self.listener.available:
            self.talk_btn.configure(state="disabled", text="Voz off")
            self._log("Voz por microfone off. Instale: pip install SpeechRecognition pyaudio", "sys")
        if not self.speaker.available:
            self._log("Sintese de voz off. Instale: pip install pyttsx3", "sys")

    def _render_convos(self) -> None:
        for w in self.convo_list.winfo_children():
            w.destroy()
        for item in self.convos:
            active = item["id"] == self.active_id
            row = tk.Frame(self.convo_list, bg=COLORS["panel"] if active else COLORS["sidebar"])
            row.pack(fill="x", pady=2)
            label = item.get("title") or "Nova conversa"
            btn = tk.Button(row, text=label[:26], anchor="w",
                            command=lambda i=item["id"]: self._select_convo(i),
                            bg=COLORS["panel"] if active else COLORS["sidebar"],
                            fg=COLORS["text"] if active else COLORS["muted"],
                            font=("Segoe UI", 9, "bold" if active else "normal"),
                            relief="flat", padx=8, pady=7, cursor="hand2",
                            activebackground=COLORS["panel"], activeforeground=COLORS["text"])
            btn.pack(side="left", fill="x", expand=True)
            tk.Button(row, text="🗑", command=lambda i=item["id"]: self._delete_convo(i),
                      bg=row["bg"], fg=COLORS["muted"], relief="flat", padx=4, cursor="hand2",
                      activebackground=COLORS["panel"]).pack(side="right")

    def _render_transcript(self) -> None:
        item = self._current()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        for entry in (item.get("log") if item else []) or []:
            tag = entry.get("r", "kemy")
            prefix = {"user": "Voce: ", "kemy": "Kemy: "}.get(tag, "")
            self.transcript.insert("end", f"{prefix}{entry.get('t', '')}\n\n", tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _select_convo(self, cid: str) -> None:
        if self.busy or cid == self.active_id:
            return
        self.active_id = cid
        self.speaker.stop()
        item = self._current()
        self.api.session_id = item.get("session_id") if item else None
        self._save_convos()
        self._render_convos()
        self._render_transcript()

    def _delete_convo(self, cid: str) -> None:
        if self.busy:
            return
        if not messagebox.askyesno("Apagar conversa", "Remover esta conversa da lista? (os arquivos no PC continuam)"):
            return
        self.convos = [c for c in self.convos if c["id"] != cid]
        if not self.convos:
            self._add_convo(select=False)
        if cid == self.active_id:
            self.active_id = self.convos[0]["id"]
            self.api.session_id = self._current().get("session_id")
        self._save_convos()
        self._render_convos()
        self._render_transcript()

    def _new_conversation(self) -> None:
        if self.busy:
            return
        self._add_convo(select=True)
        self.api.session_id = None
        self._render_convos()
        self._render_transcript()
        self._log("Nova conversa. O proximo projeto vai para uma pasta nova.", "sys")

    # ----- estado / log ----- #
    def _set_state(self, state: str) -> None:
        self.avatar.set_state(state)
        self.state_label.configure(text=STATE_LABELS.get(state, state),
                                   fg=COLORS.get(state, COLORS["muted"]))

    def _log(self, text: str, tag: str = "kemy") -> None:
        self.transcript.configure(state="normal")
        prefix = {"user": "Voce: ", "kemy": "Kemy: ", "sys": ""}.get(tag, "")
        self.transcript.insert("end", f"{prefix}{text}\n\n", tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")
        if tag in ("user", "kemy"):
            item = self._current()
            if item is not None:
                item.setdefault("log", []).append({"r": tag, "t": text})
                item["log"] = item["log"][-300:]
                item["updated"] = time.time()
                self._save_convos()

    # ----- conexao ----- #
    def _bootstrap(self) -> None:
        self.root.after(0, lambda: self._log("Iniciando Kemy local...", "sys"))
        if self.env_path:
            self.root.after(0, lambda: self._log(f"Config: {self.env_path}", "sys"))
        # Modo direto: fala direto com a IA (sem o backend pesado). Mais rapido e obedece.
        if self.mode == "direct":
            self.connected = True
            self.root.after(0, lambda: self._set_state("idle"))
            self.root.after(0, lambda: self._log(
                f"IA direta ativa ({'Gemini' if self.llm.gemini else 'Groq' if self.llm.groq else 'OpenAI'}). "
                f"Pasta: {self.workspace_root}", "sys"))
            self.speaker.say("Oi! Como posso ajudar?")
            return
        # Sem .env local: usa o Render (que ja tem as chaves). Zero config.
        self._connect_online()

    def _load_online_cfg(self) -> dict:
        try:
            return json.loads((config_dir() / "online.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_online_cfg(self) -> None:
        try:
            self.online_cfg_file.write_text(json.dumps(self.online_cfg, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _connect_online(self) -> None:
        url = self.online_cfg.get("url") or ONLINE_URL
        self.api = LocalAPI(url)
        self.root.after(0, lambda: self._log(f"Usando IA online (Render): {url}", "sys"))
        self.root.after(0, lambda: self._log("Acordando o servidor (pode levar ~30s na 1a vez)...", "sys"))
        ok = False
        for _ in range(40):
            if _healthcheck(f"{url}/api/status", timeout_seconds=4):
                ok = True
                break
            time.sleep(2)
        if not ok:
            self.root.after(0, lambda: self._log("Render nao respondeu. Verifique a internet/URL.", "sys"))
            self.root.after(0, lambda: self._set_state("error"))
            return
        try:
            self.api.login(APP_USER, self.online_cfg.get("password") or APP_PASSWORD)
        except Exception:
            pass  # /api/comando pode funcionar sem login; senao tratamos no envio
        item = self._current()
        self.api.session_id = item.get("session_id") if item else None
        self.connected = True
        self.root.after(0, lambda: self._set_state("idle"))
        self.root.after(0, lambda: self._log(f"Pronta (online)! Arquivos salvos em: {self.workspace_root}", "sys"))
        self.speaker.say("Oi! Estou usando a IA online. Como posso ajudar?")

    def _prompt_online_password(self) -> None:
        pwd = simpledialog.askstring(
            "Kemy - senha do servidor",
            "O servidor online pediu login.\nInforme a senha (KEMY_AUTH_PASSWORD do seu Render):",
            show="*",
        )
        if not pwd:
            return
        self.online_cfg["password"] = pwd
        self.online_cfg["url"] = self.api.base_url
        self._save_online_cfg()
        try:
            self.api.login(APP_USER, pwd)
            self._log("Senha salva. Pode enviar o pedido de novo.", "sys")
        except Exception as exc:
            self._log(f"Login falhou: {exc}", "sys")

    def _connect_backend(self) -> None:
        if not _healthcheck(f"{self.base_url}/api/status"):
            self._start_server()
            if not self._wait_ready():
                self.root.after(0, lambda: self._log("Servidor nao respondeu a tempo.", "sys"))
                self.root.after(0, lambda: self._set_state("error"))
                return
        try:
            self.api.login(APP_USER, APP_PASSWORD)
            item = self._current()
            self.api.session_id = item.get("session_id") if item else None
            self.connected = True
            self.root.after(0, lambda: self._set_state("idle"))
            self.root.after(0, lambda: self._log(
                f"Pronta (modo limitado). Pasta: {self.workspace_root}", "sys"))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Falha ao conectar: {e}", "sys"))
            self.root.after(0, lambda: self._set_state("error"))

    def _backend_env(self) -> dict:
        env = dict(os.environ)
        env.update(self.env_file_vars)
        env["KEMY_AUTH_USER"] = APP_USER
        env["KEMY_AUTH_PASSWORD"] = APP_PASSWORD
        env["KEMY_AUTH_SECRET"] = APP_SECRET
        return env

    def _start_server(self) -> None:
        os.chdir(ROOT_DIR)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--serve", "--host", self.host, "--port", str(self.port)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--serve",
                   "--host", self.host, "--port", str(self.port)]
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(ROOT_DIR), env=self._backend_env(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Nao consegui subir o backend: {e}", "sys"))

    def _wait_ready(self) -> bool:
        for _ in range(90):
            if _healthcheck(f"{self.base_url}/api/status"):
                return True
            time.sleep(0.35)
        return False

    # ----- avatar (imagem) ----- #
    def _autoload_avatar(self) -> None:
        for cand in (EXE_DIR / "avatar.png", config_dir() / "avatar.png",
                     EXE_DIR / "avatar.gif", config_dir() / "avatar.gif"):
            try:
                if cand.is_file():
                    self._avatar_photo = load_photo(cand, 170)
                    self.avatar.set_image(self._avatar_photo)
                    return
            except Exception:
                continue

    # ----- acoes locais ----- #
    def _save_files_local(self, resultado: dict | None) -> tuple[Path, int, Path | None] | None:
        files = (resultado or {}).get("files") or []
        if not files:
            text = str((resultado or {}).get("raw") or (resultado or {}).get("summary") or "")
            files = extract_code_files(text)
        if not files:
            return None
        item = self._current()
        base = Path(item["project"]) if item else (self.workspace_root / "projeto")
        saved = 0
        index_path: Path | None = None
        for f in files:
            rel = str(f.get("path") or "").strip().lstrip("/\\")
            content = f.get("content")
            if not rel or content is None:
                continue
            dest = base / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8", errors="ignore")
                saved += 1
                if index_path is None and rel.lower().endswith((".html", ".htm")):
                    index_path = dest
            except Exception:
                continue
        if saved:
            return base, saved, index_path
        return None

    def _open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            self._log(f"Nao consegui abrir {path}: {exc}", "sys")

    def _open_workspace(self) -> None:
        item = self._current()
        target = Path(item["project"]) if item else self.workspace_root
        if not target.exists():
            target = self.workspace_root
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._open_path(target)

    def _import_env(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecione seu .env (com as chaves de IA)",
            filetypes=[("Arquivo .env", ".env"), ("Todos", "*.*")],
        )
        if not path:
            return
        dest = config_dir() / ".env"
        try:
            shutil.copyfile(path, dest)
        except Exception as exc:
            messagebox.showerror("Kemy", f"Falha ao salvar config: {exc}")
            return
        self.env_path = dest
        self.env_file_vars = parse_env_file(dest)
        self.workspace_root = self._resolve_workspace_root()
        self.llm = LLMClient(self.env_file_vars)
        self.mode = "direct" if self.llm.available else "online"
        self._log("Config salva. Reconectando com IA...", "sys")
        if self.mode == "direct":
            self.connected = True
            self._set_state("idle")
            self._log("IA direta ativa (chaves locais).", "sys")
        else:
            threading.Thread(target=self._connect_online, daemon=True).start()

    def _restart_backend(self) -> None:
        self.connected = False
        self.root.after(0, lambda: self._set_state("offline"))
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=6)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None
        time.sleep(1.2)
        self._bootstrap()

    # ----- update ----- #
    def _check_update(self) -> None:
        threading.Thread(target=self._do_update, daemon=True).start()

    def _do_update(self) -> None:
        self.root.after(0, lambda: self._log("Procurando atualizacao...", "sys"))
        try:
            req = urllib.request.Request(
                RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "KemyDesktop"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
            url = None
            for asset in (data.get("assets") or []):
                if str(asset.get("name", "")).lower().endswith(".zip"):
                    url = asset.get("browser_download_url")
                    break
            if not url:
                raise RuntimeError("Release sem .zip.")
            if not getattr(sys, "frozen", False):
                self.root.after(0, lambda: self._log("Update so no .exe. Abrindo Releases...", "sys"))
                webbrowser.open(RELEASES_URL)
                return
            self.root.after(0, lambda: self._log("Baixando nova versao...", "sys"))
            tmp = Path(tempfile.mkdtemp(prefix="kemy_upd_"))
            zip_path = tmp / "update.zip"
            urllib.request.urlretrieve(url, zip_path)
            extract = tmp / "new"
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract)
            self.root.after(0, lambda: self._apply_update_and_restart(extract))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Falha no update: {e}. Abrindo Releases...", "sys"))
            try:
                webbrowser.open(RELEASES_URL)
            except Exception:
                pass

    def _apply_update_and_restart(self, extract: Path) -> None:
        bat = Path(tempfile.gettempdir()) / "kemy_update.bat"
        target = str(EXE_DIR)
        exe = str(EXE_DIR / "KemyDesktop.exe")
        bat.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'robocopy "{extract}" "{target}" /E /IS /IT /NFL /NDL /NJH /NJS >nul\r\n'
            f'start "" "{exe}"\r\n'
            f'rmdir /s /q "{extract.parent}"\r\n',
            encoding="utf-8",
        )
        self._log("Atualizando e reiniciando...", "sys")
        try:
            subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000008)
            self.root.after(500, self.on_close)
        except Exception as exc:
            self._log(f"Nao consegui aplicar o update: {exc}", "sys")

    # ----- interacao ----- #
    def _on_talk(self) -> None:
        if not self.connected or self.busy:
            return
        self.speaker.stop()
        self.listener.listen_once(
            on_state=lambda s: self.root.after(0, lambda: self._set_state(s)),
            on_text=lambda txt: self.root.after(0, lambda: self._handle_user_text(txt)),
            on_error=lambda e: self.root.after(0, lambda: self._on_listen_error(e)),
        )

    def _on_listen_error(self, message: str) -> None:
        self._log(message, "sys")
        self._set_state("idle" if self.connected else "offline")
        if self.continuous and self.connected and not self.busy:
            self.root.after(600, self._on_talk)

    def _submit_text(self) -> None:
        text = self.text_var.get().strip()
        if not text or self.busy:
            return
        self.text_var.set("")
        self._handle_user_text(text)

    def _handle_user_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._set_state("idle")
            return
        item = self._current()
        if item is not None and (item.get("title") in (None, "", "Nova conversa")):
            item["title"] = text[:40]
            self._render_convos()
        self._log(text, "user")
        if not self.connected:
            self._log("Ainda nao estou conectada ao backend local.", "sys")
            return
        self.busy = True
        self._set_state("thinking")
        threading.Thread(target=self._run_command, args=(text,), daemon=True).start()

    def _run_command(self, text: str) -> None:
        if self.mode == "direct":
            self._run_direct(text)
        else:
            self._run_backend(text)

    def _build_messages(self, item: dict | None) -> list[dict]:
        msgs: list[dict] = []
        for entry in (item.get("log") if item else []) or []:
            role = "assistant" if entry.get("r") == "kemy" else "user"
            txt = entry.get("t", "")
            if txt:
                msgs.append({"role": role, "content": txt})
        return msgs[-10:]

    def _run_direct(self, text: str) -> None:
        try:
            item = self._current()
            base = Path(item["project"]) if item else (self.workspace_root / "projeto")
            system = SYSTEM_PROMPT
            current = read_project_files(base)
            if current:
                system += ("\n\nARQUIVOS ATUAIS DO PROJETO (edite estes, nao recomece):\n" + current)
            messages = self._build_messages(item)
            reply = self.llm.chat(system, messages)
            files, chat = parse_llm_files(reply)
            display = chat or "Feito."
            spoken = chat or "Pronto."
            saved = self._write_files(files, base) if files else None
            if saved:
                count, index_path = saved
                display += f"\n\n💾 {count} arquivo(s) em: {base}"
                spoken = (chat + f" Salvei {count} arquivos.") if chat else f"Pronto, salvei {count} arquivos."
                if index_path is not None:
                    try:
                        webbrowser.open(index_path.as_uri())
                    except Exception:
                        pass
            commands = extract_run_commands(reply)
            self.root.after(0, lambda: self._deliver_response(spoken[:600], display, commands))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._deliver_response(
                "Falhei ao falar com a IA.", f"Erro: {e}", []))

    def _write_files(self, files: list[dict], base: Path) -> tuple[int, Path | None] | None:
        saved = 0
        index_path: Path | None = None
        for f in files:
            rel = str(f.get("path") or "").strip().lstrip("/\\")
            content = f.get("content")
            if not rel or content is None:
                continue
            dest = base / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8", errors="ignore")
                saved += 1
                if index_path is None and rel.lower().endswith((".html", ".htm")):
                    index_path = dest
            except Exception:
                continue
        return (saved, index_path) if saved else None

    def _run_backend(self, text: str) -> None:
        try:
            item = self._current()
            sid = item.get("session_id") if item else None
            try:
                if not sid:
                    sid = self.api.new_session()
                    if item is not None:
                        item["session_id"] = sid
                        self._save_convos()
                self.api.session_id = sid
                queued = self.api.send_command(text, sid, modo="coding")
            except urllib.error.HTTPError as he:
                if he.code in (401, 403) and self.mode == "online":
                    self.root.after(0, self._prompt_online_password)
                    raise RuntimeError("O servidor pediu login. Informe a senha e envie de novo.")
                raise
            sid = queued.get("session_id") or sid
            if item is not None:
                item["session_id"] = sid
            job_id = queued.get("job_id")
            if not job_id:
                raise RuntimeError("Backend nao retornou job_id.")
            resultado, erro = None, None
            for _ in range(600):
                job = self.api.job_status(job_id)
                status = job.get("status")
                if status in ("done", "error", "canceled"):
                    resultado = job.get("resultado")
                    erro = job.get("erro") if status == "error" else (
                        "Execucao cancelada." if status == "canceled" else None)
                    break
                time.sleep(0.5)
            spoken, display = result_to_speech(resultado, erro)
            saved = self._save_files_local(resultado) if not erro else None
            if saved:
                base, count, index_path = saved
                display += f"\n\n💾 {count} arquivo(s) salvos em: {base}"
                spoken = f"Pronto! Salvei {count} arquivos na pasta do projeto. " + spoken
                if index_path is not None:
                    try:
                        webbrowser.open(index_path.as_uri())
                    except Exception:
                        pass
            commands = extract_run_commands(display) if not erro else []
            self.root.after(0, lambda: self._deliver_response(spoken, display, commands))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._deliver_response(
                "Tive um problema ao falar com o backend.", f"Erro: {e}", []))

    def _deliver_response(self, spoken: str, display: str, commands: list[str] | None = None) -> None:
        self.busy = False
        self._log(display, "kemy")
        if self.speaker.available:
            self.speaker.say(spoken)
            self._set_state("speaking")
        else:
            self._set_state("idle")
            if self.continuous and self.connected:
                self.root.after(700, self._on_talk)
        if commands:
            self.root.after(300, lambda: self._handle_actions(commands))

    # ----- execucao de comandos ----- #
    def _handle_actions(self, commands: list[str]) -> None:
        autonomous = self.autonomous_var.get()
        to_run: list[str] = []
        for cmd in commands:
            if autonomous or messagebox.askyesno(
                    "Kemy quer executar no seu PC", f"Posso rodar este comando?\n\n{cmd}"):
                to_run.append(cmd)
        if to_run:
            threading.Thread(target=self._exec_commands, args=(to_run,), daemon=True).start()

    def _exec_commands(self, commands: list[str]) -> None:
        item = self._current()
        cwd = Path(item["project"]) if item else self.workspace_root
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except Exception:
            cwd = self.workspace_root
        for cmd in commands:
            self.root.after(0, lambda c=cmd: self._log(f"$ {c}", "sys"))
            try:
                proc = subprocess.run(cmd, shell=True, cwd=str(cwd),
                                      capture_output=True, text=True, timeout=180)
                out = ((proc.stdout or "") + (proc.stderr or "")).strip() or f"(codigo {proc.returncode})"
                if len(out) > 1200:
                    out = out[:1200] + "..."
                self.root.after(0, lambda o=out: self._log(o, "sys"))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self._log(f"Falha ao executar: {e}", "sys"))
        self.speaker.say("Comando executado.")

    def _on_speech_done(self) -> None:
        self._set_state("idle" if self.connected else "offline")
        if self.continuous and self.connected and not self.busy:
            self.root.after(700, self._on_talk)

    def _toggle_continuous(self) -> None:
        self.continuous = self.continuous_var.get()
        if self.continuous and self.connected and not self.busy:
            self._on_talk()

    def on_close(self) -> None:
        self.continuous = False
        self.speaker.stop()
        self._save_convos()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=6)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kemy Desktop - assistente de voz local")
    parser.add_argument("--serve", action="store_true", help="Executa apenas o backend FastAPI.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.serve:
        run_server(args.host, args.port)
        return 0
    os.chdir(ROOT_DIR)
    root = tk.Tk()
    app = KemyVoiceApp(root, host=args.host, port=args.port)
    root.mainloop()
    _ = app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
