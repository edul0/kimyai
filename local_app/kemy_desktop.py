from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def run_server(host: str, port: int) -> None:
    os.chdir(ROOT_DIR)
    import uvicorn

    uvicorn.run("agencia_kemy:app", host=host, port=port, reload=False, log_level="info")


def _healthcheck(url: str, timeout_seconds: float = 1.8) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return 200 <= int(response.status) < 500
    except Exception:
        return False


class KemyDesktopApp:
    def __init__(self, root: tk.Tk, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.root = root
        self.host = host
        self.port = int(port)
        self.process: subprocess.Popen[str] | None = None
        self.base_url = f"http://{self.host}:{self.port}"
        self.status_var = tk.StringVar(value="Status: parado")

        self.root.title("Kemy Desktop Local")
        self.root.geometry("480x240")
        self.root.minsize(460, 220)

        frame = tk.Frame(root, padx=18, pady=18)
        frame.pack(fill="both", expand=True)

        title = tk.Label(frame, text="Kemy Local", font=("Segoe UI", 17, "bold"))
        title.pack(anchor="w")

        subtitle = tk.Label(
            frame,
            text="Backend e UI rodando no seu PC, sem Render.",
            font=("Segoe UI", 10),
            fg="#4b5563",
        )
        subtitle.pack(anchor="w", pady=(4, 14))

        status = tk.Label(frame, textvariable=self.status_var, font=("Segoe UI", 10, "bold"))
        status.pack(anchor="w", pady=(0, 12))

        buttons = tk.Frame(frame)
        buttons.pack(fill="x", pady=(0, 8))

        self.start_btn = tk.Button(buttons, text="Iniciar Kemy Local", command=self.start, width=18)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.open_btn = tk.Button(buttons, text="Abrir Interface", command=self.open_ui, width=14)
        self.open_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = tk.Button(buttons, text="Parar", command=self.stop, width=10)
        self.stop_btn.pack(side="left")

        docs_row = tk.Frame(frame)
        docs_row.pack(fill="x", pady=(6, 0))
        tk.Button(docs_row, text="API Docs", command=self.open_docs, width=14).pack(side="left")

        self.logs = tk.Text(frame, height=6, wrap="word")
        self.logs.pack(fill="both", expand=True, pady=(10, 0))
        self._log("Launcher pronto.")
        self._log(f"Pasta do projeto: {ROOT_DIR}")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._refresh_buttons()

    def _cmd_for_backend(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--serve", "--host", self.host, "--port", str(self.port)]
        return [sys.executable, str(Path(__file__).resolve()), "--serve", "--host", self.host, "--port", str(self.port)]

    def _log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.logs.insert("end", f"[{timestamp}] {message}\n")
        self.logs.see("end")

    def _refresh_buttons(self) -> None:
        running = self.process is not None and self.process.poll() is None
        self.start_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _wait_ready_and_open(self) -> None:
        status_url = f"{self.base_url}/api/status"
        for _ in range(90):
            if _healthcheck(status_url):
                self.status_var.set("Status: online")
                self._log(f"Servidor online em {self.base_url}")
                try:
                    webbrowser.open(self.base_url)
                except Exception:
                    pass
                self.root.after(0, self._refresh_buttons)
                return
            time.sleep(0.35)
        self.status_var.set("Status: inicializado (sem resposta)")
        self._log("Servidor iniciado, mas /api/status nao respondeu no tempo esperado.")
        self.root.after(0, self._refresh_buttons)

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self._log("Servidor ja esta em execucao.")
            return
        os.chdir(ROOT_DIR)
        cmd = self._cmd_for_backend()
        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception as exc:
            messagebox.showerror("Erro ao iniciar", str(exc))
            self._log(f"Falha ao iniciar: {exc}")
            return
        self.status_var.set("Status: iniciando...")
        self._log(f"Iniciando backend local ({self.host}:{self.port})")
        self._refresh_buttons()
        threading.Thread(target=self._wait_ready_and_open, daemon=True).start()

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            self.status_var.set("Status: parado")
            self._log("Servidor ja estava parado.")
            self._refresh_buttons()
            return
        self._log("Encerrando backend...")
        self.process.terminate()
        try:
            self.process.wait(timeout=6)
        except Exception:
            self.process.kill()
        self.process = None
        self.status_var.set("Status: parado")
        self._refresh_buttons()

    def open_ui(self) -> None:
        webbrowser.open(self.base_url)
        self._log("Interface aberta no navegador.")

    def open_docs(self) -> None:
        webbrowser.open(f"{self.base_url}/docs")
        self._log("Swagger aberto no navegador.")

    def on_close(self) -> None:
        self.stop()
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kemy Desktop Local launcher")
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
    app = KemyDesktopApp(root, host=args.host, port=args.port)
    root.mainloop()
    _ = app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
