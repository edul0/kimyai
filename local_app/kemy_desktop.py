"""Kemy Desktop Local - assistente de voz com avatar estilo VTuber.

Executavel que transforma a Kemy num companheiro local que:
  - sobe o backend FastAPI no seu PC (sem depender de Render);
  - ouve voce pelo microfone (speech-to-text);
  - responde falando em voz alta (text-to-speech);
  - mostra um personagem animado (estilo VTuber) que pisca, fala e reage;
  - executa o pedido usando os agentes locais e mostra o resultado em tempo real.

As bibliotecas de voz (SpeechRecognition / pyttsx3 / pyaudio) sao opcionais:
sem elas o app funciona por texto e avisa como habilitar a voz.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

import tkinter as tk
from tkinter import scrolledtext


if getattr(sys, "frozen", False):
    ROOT_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# Credenciais locais: o app injeta estas variaveis no backend que ele sobe,
# entao cliente e servidor sempre concordam (env var > .env no pydantic-settings).
APP_USER = os.environ.get("KEMY_AUTH_USER", "admin")
APP_PASSWORD = os.environ.get("KEMY_AUTH_PASSWORD", "kemy-ai")
APP_SECRET = os.environ.get("KEMY_AUTH_SECRET", "kemy-local-desktop-secret")

# Paleta (dark, com acentos vivos por estado).
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
    # Cores do personagem.
    "skin": "#ffe2d2",
    "skin_shadow": "#f7c9b6",
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
# Dependencias opcionais de voz
# --------------------------------------------------------------------------- #
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
# Backend FastAPI (modo --serve)
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
# Avatar VTuber animado (Canvas com personagem)
# --------------------------------------------------------------------------- #
class Avatar:
    def __init__(self, parent: tk.Widget, size: int = 300) -> None:
        self.size = size
        self.canvas = tk.Canvas(parent, width=size, height=size, bg=COLORS["panel"],
                                highlightthickness=0)
        self.state = "idle"
        self.t = 0.0
        self._blink_t = 0.0
        self._build()
        self._animate()

    # cria todos os itens uma vez (atualizamos coords no loop -> sem flicker)
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
        # balanco suave (idle); mais "alerta" quando ouvindo
        sway = math.sin(t * 1.1) * (5 if self.state in ("idle", "speaking") else 2)
        bob = math.sin(t * 0.9) * 4
        cy = S * 0.52 + bob
        cx += sway

        rx, ry = S * 0.24, S * 0.26  # cabeca

        # aura/glow externo
        gr = rx + 34 + (6 * (0.5 + 0.5 * math.sin(t * 2)) if self.state in ("listening", "speaking") else 0)
        self._ov(c, self.glow, cx, cy, gr, gr)
        c.itemconfig(self.glow, outline=accent,
                     width=3 if self.state in ("listening", "speaking", "thinking") else 1)

        # cabelo de tras
        bw, bh = rx * 1.45, ry * 1.5
        c.coords(self.back_hair,
                 cx - bw, cy - bh * 0.5,
                 cx - bw * 0.7, cy + bh,
                 cx, cy + bh * 1.15,
                 cx + bw * 0.7, cy + bh,
                 cx + bw, cy - bh * 0.5,
                 cx + bw * 0.4, cy - bh,
                 cx - bw * 0.4, cy - bh)

        # cabeca
        self._ov(c, self.head, cx, cy, rx, ry)

        # orelhas/fones
        ear_y = cy + ry * 0.1
        ex = rx * 1.02
        self._ov(c, self.ear_l, cx - ex, ear_y, S * 0.045, S * 0.055)
        self._ov(c, self.ear_r, cx + ex, ear_y, S * 0.045, S * 0.055)
        cup_pulse = (3 * (0.5 + 0.5 * math.sin(t * 4))) if self.state == "listening" else 0
        self._ov(c, self.cup_l, cx - ex, ear_y, S * 0.06 + cup_pulse, S * 0.075 + cup_pulse)
        self._ov(c, self.cup_r, cx + ex, ear_y, S * 0.06 + cup_pulse, S * 0.075 + cup_pulse)
        c.itemconfig(self.cup_l, fill=accent)
        c.itemconfig(self.cup_r, fill=accent)

        # arco do headphone por cima
        c.coords(self.band, cx - ex - S * 0.02, cy - ry - S * 0.05,
                 cx + ex + S * 0.02, cy + ry * 0.2)
        c.itemconfig(self.band, outline=accent, start=10, extent=160)

        # franja
        fy = cy - ry * 0.55
        c.coords(self.bangs,
                 cx - rx, cy - ry * 0.2,
                 cx - rx * 0.95, fy - ry * 0.4,
                 cx - rx * 0.3, cy - ry,
                 cx, fy,
                 cx + rx * 0.3, cy - ry,
                 cx + rx * 0.95, fy - ry * 0.4,
                 cx + rx, cy - ry * 0.2,
                 cx + rx * 0.5, cy - ry * 0.35,
                 cx, cy - ry * 0.15,
                 cx - rx * 0.5, cy - ry * 0.35)
        # mecha central (aho)
        c.coords(self.tuft,
                 cx - 6, cy - ry * 0.98,
                 cx + 2, cy - ry * 1.28,
                 cx + 10, cy - ry * 0.98)

        # olhos (com piscar)
        blink = self._blink_factor(t)
        eye_y = cy + ry * 0.05
        eye_dx = rx * 0.46
        ew, eh = S * 0.052, S * 0.066 * blink
        self._ov(c, self.eye_l, cx - eye_dx, eye_y, ew, max(eh, 1))
        self._ov(c, self.eye_r, cx + eye_dx, eye_y, ew, max(eh, 1))

        # iris: olha pra cima quando "pensando"
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

        # sobrancelhas
        by = eye_y - eh - S * 0.03
        c.coords(self.brow_l, cx - eye_dx - ew, by + 2, cx - eye_dx + ew, by)
        c.coords(self.brow_r, cx + eye_dx - ew, by, cx + eye_dx + ew, by + 2)

        # blush
        self._ov(c, self.blush_l, cx - eye_dx - S * 0.01, eye_y + S * 0.07, S * 0.03, S * 0.018)
        self._ov(c, self.blush_r, cx + eye_dx + S * 0.01, eye_y + S * 0.07, S * 0.03, S * 0.018)

        # boca: anima abrindo quando falando
        my = cy + ry * 0.5
        if self.state == "speaking":
            open_amt = (0.5 + 0.5 * math.sin(t * 16)) * S * 0.03 + S * 0.006
        else:
            open_amt = S * 0.006
        self._ov(c, self.mouth, cx, my, S * 0.028, open_amt)

        # pontinhos de "pensando"
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
        # pisca rapido a cada ~3.2s
        cycle = t % 3.2
        if cycle < 0.14:
            return max(0.05, abs(math.cos(cycle / 0.14 * math.pi)))
        return 1.0


# --------------------------------------------------------------------------- #
# Voz: TTS + STT
# --------------------------------------------------------------------------- #
class Speaker:
    def __init__(self) -> None:
        self.available = VOICE_SUPPORT["tts"]
        self.on_start = None
        self.on_done = None
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._engine = None
        if self.available:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        try:
            import pyttsx3

            engine = pyttsx3.init()
            self._engine = engine
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
            if self.on_start:
                self.on_start()
            try:
                engine.say(text)
                engine.runAndWait()
            except Exception:
                pass
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

        self.speaker = Speaker()
        self.speaker.on_start = lambda: self.root.after(0, lambda: self._set_state("speaking"))
        self.speaker.on_done = lambda: self.root.after(0, self._on_speech_done)
        self.listener = Listener()

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._set_state("offline")
        threading.Thread(target=self._bootstrap, daemon=True).start()

    def _build_ui(self) -> None:
        self.root.title("Kemy - Assistente de Voz")
        self.root.geometry("820x680")
        self.root.minsize(720, 600)
        self.root.configure(bg=COLORS["bg"])

        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=24, pady=(18, 4))
        tk.Label(header, text="Kemy", font=("Segoe UI", 22, "bold"),
                 fg=COLORS["text"], bg=COLORS["bg"]).pack(side="left")
        tk.Label(header, text="  sua VTuber assistente local", font=("Segoe UI", 10),
                 fg=COLORS["muted"], bg=COLORS["bg"]).pack(side="left", pady=(10, 0))
        self.state_label = tk.Label(header, text=STATE_LABELS["offline"],
                                    font=("Segoe UI", 11, "bold"),
                                    fg=COLORS["muted"], bg=COLORS["bg"])
        self.state_label.pack(side="right", pady=(8, 0))

        self.avatar = Avatar(self.root, size=300)
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
        controls.pack(fill="x", padx=24, pady=(2, 18))
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
            self.root.after(0, lambda: self._log("Tudo pronto! Fale comigo ou escreva um pedido.", "sys"))
            self.speaker.say("Oi! Eu sou a Kemy. Como posso te ajudar?")
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Falha ao conectar: {e}", "sys"))
            self.root.after(0, lambda: self._set_state("error"))

    def _backend_env(self) -> dict:
        env = dict(os.environ)
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
