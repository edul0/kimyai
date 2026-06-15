"""Kemy Desktop Local - assistente de voz com avatar estilo VTuber.

Executavel que transforma a Kemy num companheiro local que:
  - sobe o backend FastAPI no seu PC (sem depender de Render);
  - usa SUAS chaves de IA (carregadas de um .env local) -> respostas reais;
  - ouve voce pelo microfone (speech-to-text);
  - responde falando, com a boca sincronizada em tempo real (text-to-speech);
  - mostra um personagem animado (estilo VTuber) que pisca, fala e reage;
  - SALVA os arquivos gerados direto numa pasta do seu PC (acao local real);
  - checa atualizacoes e aponta para a ultima versao publicada.

As bibliotecas de voz (SpeechRecognition / pyttsx3 / pyaudio) sao opcionais.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import http.cookiejar
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext


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

# Link fixo para a ultima versao publicada (GitHub Release).
RELEASES_URL = "https://github.com/edul0/kimyai/releases"

COLORS = {
    "bg": "#0a0f16",
    "panel": "#121a26",
    "panel_soft": "#0f1620",
    "line": "#243246",
    "text": "#e8eefc",
    "muted": "#8ea0bd",
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
    "idle": "Pronta",
    "listening": "Ouvindo...",
    "thinking": "Pensando...",
    "speaking": "Falando...",
    "offline": "Conectando...",
    "error": "Erro",
}


# --------------------------------------------------------------------------- #
# Config local (.env) e pasta de trabalho
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
    candidates = [
        EXE_DIR / ".env",
        config_dir() / ".env",
        ROOT_DIR / ".env",
        Path.cwd() / ".env",
    ]
    for cand in candidates:
        try:
            if cand.is_file():
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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and not key.startswith("#"):
            data[key] = value
    return data


def _detect_voice_support() -> dict[str, bool]:
    support = {"tts": False, "stt": False}
    try:
        import pyttsx3  # noqa: F401

        support["tts"] = True
    except Exception:
        support["tts"] = False
    try:
        import speech_recognition  # noqa: F401

        support["stt"] = True
    except Exception:
        support["stt"] = False
    return support


VOICE_SUPPORT = _detect_voice_support()


# --------------------------------------------------------------------------- #
# Backend FastAPI
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


# --------------------------------------------------------------------------- #
# Cliente HTTP local
# --------------------------------------------------------------------------- #
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
        self.session_id = data.get("session_id")
        return self.session_id or ""

    def send_command(self, message: str, modo: str = "coding") -> dict:
        payload = {"mensagem": message, "modo": modo, "session_id": self.session_id}
        data = self._request("POST", "/api/comando", payload, timeout=40)
        if data.get("session_id"):
            self.session_id = data["session_id"]
        return data

    def job_status(self, job_id: str) -> dict:
        return self._request("GET", f"/api/jobs/{job_id}", None, timeout=30)


def result_to_speech(resultado: dict | None, erro: str | None) -> tuple[str, str]:
    if erro:
        return ("Tive um problema ao executar o pedido.", f"Erro: {erro}")
    if not resultado:
        return ("Concluido.", "Concluido.")
    raw = str(resultado.get("raw") or "").strip()
    summary = str(resultado.get("summary") or "").strip()
    files = resultado.get("files") or []
    if files and not summary and not raw:
        names = ", ".join(str(f.get("path", "")) for f in files[:4])
        spoken = f"Gerei {len(files)} arquivo. {names}."
        return (spoken, spoken)
    display = raw or summary or "Concluido."
    spoken = summary or raw or "Concluido."
    if len(spoken) > 600:
        spoken = spoken[:600].rsplit(" ", 1)[0] + "..."
    return (spoken, display)


# --------------------------------------------------------------------------- #
# Avatar VTuber animado
# --------------------------------------------------------------------------- #
class Avatar:
    def __init__(self, parent: tk.Widget, size: int = 300) -> None:
        self.size = size
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=COLORS["panel"],
                                highlightthickness=0)
        self.state = "idle"
        self.t = 0.0
        self.mouth_provider = None  # callable -> 0..1 (nivel da boca em tempo real)
        self._build()
        self._animate()

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

        gr = rx + 34 + (6 * (0.5 + 0.5 * math.sin(t * 2)) if self.state in ("listening", "speaking") else 0)
        self._ov(c, self.glow, cx, cy, gr, gr)
        c.itemconfig(self.glow, outline=accent,
                     width=3 if self.state in ("listening", "speaking", "thinking") else 1)

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

        # boca: tempo real via mouth_provider (eventos de palavra do TTS)
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
# Voz: TTS com lip-sync por palavra + STT
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

    def _on_word(self, *_args, **_kwargs) -> None:
        # Disparado a cada palavra falada -> alimenta o lip-sync em tempo real.
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
                self._mic_ok = False

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
# App principal
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
        self.last_saved_dir: Path | None = None

        # Carrega .env do usuario (chaves de IA, LLM_MODE, workspace...).
        self.env_path = find_env_file()
        self.env_file_vars = parse_env_file(self.env_path) if self.env_path else {}
        self.workspace_root = self._resolve_workspace_root()

        self.speaker = Speaker()
        self.speaker.on_start = lambda: self.root.after(0, lambda: self._set_state("speaking"))
        self.speaker.on_done = lambda: self.root.after(0, self._on_speech_done)
        self.listener = Listener()

        self._build_ui()
        self.avatar.mouth_provider = self.speaker.mouth_level  # lip-sync em tempo real
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_state("offline")
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _resolve_workspace_root(self) -> Path:
        raw = self.env_file_vars.get("KEMY_LOCAL_WORKSPACE_ROOT", "").strip()
        if raw:
            try:
                return Path(raw)
            except Exception:
                pass
        return Path.home() / "KemyWorkspace"

    def _has_ai_keys(self) -> bool:
        keys = ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
        return any(self.env_file_vars.get(k) for k in keys)

    # ----- UI ----- #
    def _build_ui(self) -> None:
        self.root.title("Kemy - Assistente de Voz")
        self.root.geometry("840x720")
        self.root.minsize(740, 620)
        self.root.configure(bg=COLORS["bg"])

        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=24, pady=(16, 4))
        tk.Label(header, text="Kemy", font=("Segoe UI", 22, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(header, text="  sua VTuber assistente local", font=("Segoe UI", 10),
                 fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left", pady=(10, 0))
        self.state_label = tk.Label(header, text=STATE_LABELS["offline"],
                                    font=("Segoe UI", 11, "bold"),
                                    fg=COLORS["muted"], bg=COLORS["bg"])
        self.state_label.pack(side="right", pady=(8, 0))

        # barra de ferramentas (config / pasta / atualizar)
        tools = tk.Frame(self.root, bg=COLORS["bg"])
        tools.pack(fill="x", padx=24)
        for txt, cmd in (("⚙ Configurar IA (.env)", self._import_env),
                         ("📁 Abrir pasta", self._open_workspace),
                         ("⬆ Atualizar", self._check_update)):
            tk.Button(tools, text=txt, command=cmd, bg=COLORS["panel"], fg=COLORS["muted"],
                      font=("Segoe UI", 9), relief="flat", padx=10, pady=4,
                      cursor="hand2").pack(side="left", padx=(0, 8))

        self.avatar = Avatar(self.root, size=290)
        self.avatar.canvas.pack(pady=(4, 6))

        self.transcript = scrolledtext.ScrolledText(
            self.root, height=8, wrap="word", font=("Segoe UI", 10),
            bg=COLORS["panel_soft"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", padx=14, pady=12, borderwidth=0,
        )
        self.transcript.pack(fill="both", expand=True, padx=24, pady=6)
        self.transcript.tag_config("user", foreground=COLORS["user"], font=("Segoe UI", 10, "bold"))
        self.transcript.tag_config("kemy", foreground=COLORS["text"])
        self.transcript.tag_config("sys", foreground=COLORS["muted"], font=("Segoe UI", 9, "italic"))
        self.transcript.configure(state="disabled")

        entry_row = tk.Frame(self.root, bg=COLORS["bg"])
        entry_row.pack(fill="x", padx=24, pady=(0, 6))
        self.text_var = tk.StringVar()
        self.entry = tk.Entry(entry_row, textvariable=self.text_var, font=("Segoe UI", 11),
                              bg=COLORS["panel"], fg=COLORS["text"], insertbackground=COLORS["text"],
                              relief="flat")
        self.entry.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._submit_text())
        tk.Button(entry_row, text="Enviar", command=self._submit_text,
                  bg=COLORS["idle"], fg=COLORS["bg"], font=("Segoe UI", 10, "bold"),
                  relief="flat", activebackground=COLORS["speaking"], padx=16, pady=7,
                  cursor="hand2").pack(side="left")

        controls = tk.Frame(self.root, bg=COLORS["bg"])
        controls.pack(fill="x", padx=24, pady=(2, 16))
        self.talk_btn = tk.Button(controls, text="🎙  Falar com a Kemy", command=self._on_talk,
                                  bg=COLORS["listening"], fg=COLORS["bg"],
                                  font=("Segoe UI", 11, "bold"), relief="flat",
                                  activebackground=COLORS["speaking"], padx=20, pady=11,
                                  cursor="hand2")
        self.talk_btn.pack(side="left")
        self.continuous_var = tk.BooleanVar(value=False)
        tk.Checkbutton(controls, text="Modo conversa (escuta continua)",
                       variable=self.continuous_var, command=self._toggle_continuous,
                       bg=COLORS["bg"], fg=COLORS["muted"], selectcolor=COLORS["panel"],
                       activebackground=COLORS["bg"], activeforeground=COLORS["text"],
                       font=("Segoe UI", 9)).pack(side="left", padx=14)
        tk.Button(controls, text="Silenciar", command=self.speaker.stop,
                  bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 10),
                  relief="flat", padx=14, pady=9, cursor="hand2").pack(side="right")

        if not self.listener.available:
            self.talk_btn.configure(state="disabled", text="Voz indisponivel")
            self._log("Voz por microfone desativada. Instale: pip install SpeechRecognition pyaudio", "sys")
        if not self.speaker.available:
            self._log("Sintese de voz desativada. Instale: pip install pyttsx3", "sys")

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

    # ----- conexao ----- #
    def _bootstrap(self) -> None:
        self.root.after(0, lambda: self._log("Iniciando Kemy local...", "sys"))
        if self.env_path:
            self.root.after(0, lambda: self._log(f"Config carregada de: {self.env_path}", "sys"))
            if self._has_ai_keys() and self.env_file_vars.get("LLM_MODE", "").lower() != "mock":
                self.root.after(0, lambda: self._log("IA real ativada com suas chaves.", "sys"))
            else:
                self.root.after(0, lambda: self._log("Sem chave de IA -> respostas em modo exemplo.", "sys"))
        else:
            self.root.after(0, lambda: self._log(
                "Nenhum .env encontrado. Clique em 'Configurar IA' para ativar a IA real.", "sys"))

        if not _healthcheck(f"{self.base_url}/api/status"):
            self._start_server()
            if not self._wait_ready():
                self.root.after(0, lambda: self._log("Servidor nao respondeu a tempo.", "sys"))
                self.root.after(0, lambda: self._set_state("error"))
                return
        try:
            self.api.login(APP_USER, APP_PASSWORD)
            self.api.new_session()
            self.connected = True
            self.root.after(0, lambda: self._set_state("idle"))
            self.root.after(0, lambda: self._log(
                f"Tudo pronto! Arquivos serao salvos em: {self.workspace_root}", "sys"))
            self.speaker.say("Oi! Eu sou a Kemy. Como posso te ajudar?")
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Falha ao conectar: {e}", "sys"))
            self.root.after(0, lambda: self._set_state("error"))

    def _backend_env(self) -> dict:
        env = dict(os.environ)
        env.update(self.env_file_vars)  # chaves de IA, LLM_MODE etc.
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

    # ----- acoes locais ----- #
    def _save_files_local(self, resultado: dict | None) -> tuple[Path, int] | None:
        files = (resultado or {}).get("files") or []
        if not files:
            return None
        base = self.workspace_root / ("projeto-" + time.strftime("%Y%m%d-%H%M%S"))
        saved = 0
        for item in files:
            rel = str(item.get("path") or "").strip().lstrip("/\\")
            content = item.get("content")
            if not rel or content is None:
                continue
            dest = base / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8", errors="ignore")
                saved += 1
            except Exception:
                continue
        if saved:
            self.last_saved_dir = base
            return base, saved
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
        target = self.last_saved_dir or self.workspace_root
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._open_path(target)

    def _import_env(self) -> None:
        path = filedialog.askopenfilename(
            title="Selecione seu arquivo .env (com as chaves de IA)",
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
        self._log("Config salva. Reconectando com IA real...", "sys")
        threading.Thread(target=self._restart_backend, daemon=True).start()

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

    def _check_update(self) -> None:
        self._log("Abrindo a pagina de versoes mais recentes...", "sys")
        try:
            webbrowser.open(RELEASES_URL)
        except Exception:
            pass

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
        self._log(text, "user")
        if not self.connected:
            self._log("Ainda nao estou conectada ao backend local.", "sys")
            return
        self.busy = True
        self._set_state("thinking")
        threading.Thread(target=self._run_command, args=(text,), daemon=True).start()

    def _run_command(self, text: str) -> None:
        try:
            queued = self.api.send_command(text, modo="coding")
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
                        "Execucao cancelada." if status == "canceled" else None
                    )
                    break
                time.sleep(0.5)
            spoken, display = result_to_speech(resultado, erro)
            saved = self._save_files_local(resultado) if not erro else None
            if saved:
                base, count = saved
                display += f"\n\n💾 Salvei {count} arquivo(s) em: {base}"
                spoken = f"Pronto! Criei {count} arquivos e salvei na sua pasta. " + spoken
            self.root.after(0, lambda: self._deliver_response(spoken, display))
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._deliver_response(
                "Tive um problema ao falar com o backend.", f"Erro: {e}"
            ))

    def _deliver_response(self, spoken: str, display: str) -> None:
        self.busy = False
        self._log(display, "kemy")
        if self.speaker.available:
            self.speaker.say(spoken)
            self._set_state("speaking")
        else:
            self._set_state("idle")
            if self.continuous and self.connected:
                self.root.after(700, self._on_talk)

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
