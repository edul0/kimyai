"""Kemy Desktop Local - assistente de voz com avatar animado.

Este executavel transforma a Kemy num companheiro local que:
  - sobe o backend FastAPI no seu PC (sem depender de Render);
  - ouve voce pelo microfone (speech-to-text);
  - responde falando em voz alta (text-to-speech);
  - mostra um avatar animado que reage (ocioso / ouvindo / pensando / falando);
  - executa o pedido usando os agentes locais e mostra o resultado em tempo real.

As bibliotecas de voz (SpeechRecognition / pyttsx3 / pyaudio) sao opcionais:
se nao estiverem instaladas, o app continua funcionando por texto e avisa como
habilitar a voz.
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
from tkinter import messagebox, scrolledtext


if getattr(sys, "frozen", False):
    # Dentro do .exe (PyInstaller): os dados ficam em _MEIPASS.
    ROOT_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_USER = os.environ.get("KEMY_AUTH_USER", "admin")
DEFAULT_PASSWORD = os.environ.get("KEMY_AUTH_PASSWORD", "troque-esta-senha")

# Paleta alinhada com a interface web (verde/teal escuro).
COLORS = {
    "bg": "#08120d",
    "panel": "#0d1c15",
    "panel_soft": "#11241b",
    "line": "#1c3326",
    "text": "#dff3ea",
    "muted": "#88a69a",
    "idle": "#4adfba",
    "listening": "#5ab0ff",
    "thinking": "#ffc857",
    "speaking": "#67e9c4",
    "error": "#f87171",
    "user": "#9fe7d2",
}

STATE_LABELS = {
    "idle": "Pronta",
    "listening": "Ouvindo...",
    "thinking": "Pensando...",
    "speaking": "Falando...",
    "offline": "Desconectada",
    "error": "Erro",
}


# --------------------------------------------------------------------------- #
# Deteccao de dependencias opcionais de voz
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
# Backend FastAPI (modo --serve usado pelo subprocess)
# --------------------------------------------------------------------------- #
def run_server(host: str, port: int) -> None:
    os.chdir(ROOT_DIR)
    import uvicorn

    if getattr(sys, "frozen", False):
        # No .exe importamos o objeto direto (sem depender de arquivo no disco).
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
# Cliente HTTP local (urllib + cookies, sem dependencias extras)
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
    """Devolve (texto_para_falar, texto_para_exibir)."""
    if erro:
        return ("Tive um problema ao executar o pedido.", f"Erro: {erro}")
    if not resultado:
        return ("Concluido.", "Concluido.")
    raw = str(resultado.get("raw") or "").strip()
    summary = str(resultado.get("summary") or "").strip()
    files = resultado.get("files") or []
    if files and not summary and not raw:
        names = ", ".join(str(f.get("path", "")) for f in files[:4])
        spoken = f"Gerei {len(files)} arquivo(s): {names}."
        return (spoken, spoken)
    display = raw or summary or "Concluido."
    spoken = summary or raw or "Concluido."
    # Fala um trecho objetivo para nao ficar longo demais.
    if len(spoken) > 600:
        spoken = spoken[:600].rsplit(" ", 1)[0] + "..."
    return (spoken, display)


# --------------------------------------------------------------------------- #
# Avatar animado (Canvas reativo ao estado)
# --------------------------------------------------------------------------- #
class Avatar:
    def __init__(self, parent: tk.Widget, size: int = 220) -> None:
        self.size = size
        self.center = size / 2
        self.canvas = tk.Canvas(
            parent, width=size, height=size, bg=COLORS["panel"], highlightthickness=0
        )
        self.state = "idle"
        self.phase = 0.0
        self.ring_ids: list[int] = []
        self.core_id = self.canvas.create_oval(0, 0, 0, 0, fill=COLORS["idle"], outline="")
        # Aneis de brilho desenhados atras do nucleo.
        for _ in range(3):
            ring = self.canvas.create_oval(0, 0, 0, 0, outline=COLORS["idle"], width=2)
            self.canvas.tag_lower(ring, self.core_id)
            self.ring_ids.append(ring)
        self.dot_ids = [self.canvas.create_oval(0, 0, 0, 0, fill=COLORS["idle"], outline="") for _ in range(8)]
        self._animate()

    def set_state(self, state: str) -> None:
        self.state = state if state in COLORS or state in STATE_LABELS else "idle"

    def _color(self) -> str:
        return COLORS.get(self.state, COLORS["idle"])

    def _animate(self) -> None:
        self.phase += 0.08
        color = self._color()
        c = self.center

        # O nucleo pulsa; a intensidade depende do estado.
        if self.state == "listening":
            amp, speed = 14, 2.4
        elif self.state == "speaking":
            amp, speed = 18, 3.6
        elif self.state == "thinking":
            amp, speed = 6, 1.4
        elif self.state in ("offline", "error"):
            amp, speed = 3, 0.8
        else:  # idle: respiracao suave
            amp, speed = 8, 1.0

        base = self.size * 0.20
        r = base + amp * (0.5 + 0.5 * math.sin(self.phase * speed))
        self.canvas.coords(self.core_id, c - r, c - r, c + r, c + r)
        self.canvas.itemconfig(self.core_id, fill=color)

        # Aneis concentricos expandindo (efeito de onda).
        for i, ring in enumerate(self.ring_ids):
            wave = (self.phase * speed + i * 1.1) % 3.0
            rr = r + wave * (self.size * 0.10)
            self.canvas.coords(ring, c - rr, c - rr, c + rr, c + rr)
            fade = max(0, 1 - wave / 3.0)
            width = max(1, int(3 * fade))
            self.canvas.itemconfig(ring, outline=color, width=width)

        # Pontos orbitando (mais visiveis quando pensando/ouvindo).
        show_dots = self.state in ("thinking", "listening")
        orbit = self.size * 0.40
        for i, dot in enumerate(self.dot_ids):
            if not show_dots:
                self.canvas.coords(dot, 0, 0, 0, 0)
                continue
            ang = self.phase * 1.6 + (i / len(self.dot_ids)) * math.tau
            dx = c + orbit * math.cos(ang)
            dy = c + orbit * math.sin(ang)
            ds = 4 + 2 * math.sin(self.phase * 2 + i)
            self.canvas.coords(dot, dx - ds, dy - ds, dx + ds, dy + ds)
            self.canvas.itemconfig(dot, fill=color)

        self.canvas.after(33, self._animate)


# --------------------------------------------------------------------------- #
# Voz: TTS (pyttsx3) em thread dedicada + STT (SpeechRecognition)
# --------------------------------------------------------------------------- #
class Speaker:
    """Fila de fala em thread propria (pyttsx3 nao e thread-safe)."""

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
                engine.setProperty("rate", 185)
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
    """Captura de fala sob demanda (push-to-talk) usando SpeechRecognition."""

    def __init__(self) -> None:
        self.available = VOICE_SUPPORT["stt"]
        self._recognizer = None
        self._mic_ok = False
        if self.available:
            try:
                import speech_recognition as sr

                self._recognizer = sr.Recognizer()
                self._recognizer.dynamic_energy_threshold = True
                # Confirma que existe microfone disponivel.
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
                on_error("Nao entendi o audio. Tente falar mais claro.")
            except Exception as exc:  # pragma: no cover
                on_error(f"Falha no reconhecimento: {exc}")

        threading.Thread(target=_worker, daemon=True).start()


# --------------------------------------------------------------------------- #
# Aplicacao principal
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
        # Conecta automaticamente ao abrir.
        threading.Thread(target=self._bootstrap, daemon=True).start()

    # ----- UI ----- #
    def _build_ui(self) -> None:
        self.root.title("Kemy - Assistente de Voz Local")
        self.root.geometry("760x620")
        self.root.minsize(680, 560)
        self.root.configure(bg=COLORS["bg"])

        header = tk.Frame(self.root, bg=COLORS["bg"])
        header.pack(fill="x", padx=22, pady=(18, 6))
        tk.Label(
            header, text="Kemy", font=("Segoe UI", 20, "bold"),
            fg=COLORS["text"], bg=COLORS["bg"],
        ).pack(side="left")
        self.state_label = tk.Label(
            header, text=STATE_LABELS["offline"], font=("Segoe UI", 11, "bold"),
            fg=COLORS["muted"], bg=COLORS["bg"],
        )
        self.state_label.pack(side="right")

        avatar_wrap = tk.Frame(self.root, bg=COLORS["panel"], highlightthickness=1,
                               highlightbackground=COLORS["line"])
        avatar_wrap.pack(padx=22, pady=8)
        self.avatar = Avatar(avatar_wrap, size=200)
        self.avatar.canvas.pack(padx=14, pady=14)

        self.transcript = scrolledtext.ScrolledText(
            self.root, height=10, wrap="word", font=("Segoe UI", 10),
            bg=COLORS["panel_soft"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat", padx=12, pady=10, borderwidth=0,
        )
        self.transcript.pack(fill="both", expand=True, padx=22, pady=8)
        self.transcript.tag_config("user", foreground=COLORS["user"], font=("Segoe UI", 10, "bold"))
        self.transcript.tag_config("kemy", foreground=COLORS["text"])
        self.transcript.tag_config("sys", foreground=COLORS["muted"], font=("Segoe UI", 9, "italic"))
        self.transcript.configure(state="disabled")

        # Entrada de texto (fallback / complemento da voz).
        entry_row = tk.Frame(self.root, bg=COLORS["bg"])
        entry_row.pack(fill="x", padx=22, pady=(0, 6))
        self.text_var = tk.StringVar()
        self.entry = tk.Entry(
            entry_row, textvariable=self.text_var, font=("Segoe UI", 11),
            bg=COLORS["panel_soft"], fg=COLORS["text"], insertbackground=COLORS["text"],
            relief="flat",
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=8, padx=(0, 8))
        self.entry.bind("<Return>", lambda _e: self._submit_text())
        tk.Button(
            entry_row, text="Enviar", command=self._submit_text,
            bg=COLORS["idle"], fg=COLORS["bg"], font=("Segoe UI", 10, "bold"),
            relief="flat", activebackground=COLORS["speaking"], padx=14, pady=6,
        ).pack(side="left")

        controls = tk.Frame(self.root, bg=COLORS["bg"])
        controls.pack(fill="x", padx=22, pady=(2, 16))

        self.talk_btn = tk.Button(
            controls, text="Falar com a Kemy", command=self._on_talk,
            bg=COLORS["listening"], fg=COLORS["bg"], font=("Segoe UI", 11, "bold"),
            relief="flat", activebackground=COLORS["speaking"], padx=18, pady=10,
        )
        self.talk_btn.pack(side="left")

        self.continuous_var = tk.BooleanVar(value=False)
        self.continuous_chk = tk.Checkbutton(
            controls, text="Modo conversa (escuta continua)", variable=self.continuous_var,
            command=self._toggle_continuous, bg=COLORS["bg"], fg=COLORS["muted"],
            selectcolor=COLORS["panel"], activebackground=COLORS["bg"],
            activeforeground=COLORS["text"], font=("Segoe UI", 9),
        )
        self.continuous_chk.pack(side="left", padx=12)

        self.stop_speak_btn = tk.Button(
            controls, text="Silenciar", command=self.speaker.stop,
            bg=COLORS["panel_soft"], fg=COLORS["text"], font=("Segoe UI", 10),
            relief="flat", padx=12, pady=8,
        )
        self.stop_speak_btn.pack(side="right")

        if not (self.listener.available):
            self.talk_btn.configure(state="disabled", text="Voz indisponivel")
            self.continuous_chk.configure(state="disabled")
            self._log(
                "Voz por microfone desativada. Instale: pip install SpeechRecognition pyaudio",
                "sys",
            )
        if not self.speaker.available:
            self._log("Sintese de voz desativada. Instale: pip install pyttsx3", "sys")

    # ----- helpers de estado/log ----- #
    def _set_state(self, state: str) -> None:
        self.avatar.set_state(state)
        self.state_label.configure(text=STATE_LABELS.get(state, state))
        color = COLORS.get(state, COLORS["muted"])
        self.state_label.configure(fg=color if state != "idle" else COLORS["muted"])

    def _log(self, text: str, tag: str = "kemy") -> None:
        self.transcript.configure(state="normal")
        prefix = {"user": "Voce: ", "kemy": "Kemy: ", "sys": ""}.get(tag, "")
        self.transcript.insert("end", f"{prefix}{text}\n\n", tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    # ----- conexao / backend ----- #
    def _bootstrap(self) -> None:
        self.root.after(0, lambda: self._log("Iniciando Kemy local...", "sys"))
        if not _healthcheck(f"{self.base_url}/api/status"):
            self._start_server()
            ok = self._wait_ready()
            if not ok:
                self.root.after(0, lambda: self._log("Servidor nao respondeu a tempo.", "sys"))
                self.root.after(0, lambda: self._set_state("offline"))
                return
        try:
            self.api.login(DEFAULT_USER, DEFAULT_PASSWORD)
            self.api.new_session()
            self.connected = True
            self.root.after(0, lambda: self._set_state("idle"))
            self.root.after(0, lambda: self._log("Tudo pronto. Fale comigo ou escreva um pedido.", "sys"))
            self.speaker.say("Ola, eu sou a Kemy. Como posso ajudar?")
        except Exception as exc:
            self.root.after(0, lambda e=exc: self._log(f"Falha ao conectar: {e}", "sys"))
            self.root.after(0, lambda: self._set_state("error"))

    def _start_server(self) -> None:
        os.chdir(ROOT_DIR)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--serve", "--host", self.host, "--port", str(self.port)]
        else:
            cmd = [sys.executable, str(Path(__file__).resolve()), "--serve",
                   "--host", self.host, "--port", str(self.port)]
        try:
            self.process = subprocess.Popen(
                cmd, cwd=str(ROOT_DIR),
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

    # ----- interacao de voz/texto ----- #
    def _on_talk(self) -> None:
        if not self.connected or self.busy:
            return
        self.speaker.stop()  # barge-in: para de falar quando o usuario quer falar
        self.listener.listen_once(
            on_state=lambda s: self.root.after(0, lambda: self._set_state(s)),
            on_text=lambda t: self.root.after(0, lambda: self._handle_user_text(t)),
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
            for _ in range(600):  # ~5 min max
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

    # ----- ciclo de vida ----- #
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
