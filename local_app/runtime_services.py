"""Serviços pequenos e testáveis usados pelo desktop da Kemy.

Mantém telemetria de sessão e credenciais fora do monólito da interface.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path


class ActivityTracker:
    """Histórico limitado, sem conteúdo sensível, para explicar o que a agente fez."""

    def __init__(self, limit: int = 160) -> None:
        self._items = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._last_model: dict = {}

    def record(self, kind: str, label: str, status: str = "done", **meta) -> None:
        safe = {k: v for k, v in meta.items() if k not in {"key", "token", "secret", "password"}}
        item = {"kind": kind, "label": str(label)[:240], "status": status, "at": time.time(), **safe}
        with self._lock:
            self._items.append(item)
            if kind == "model" and status == "done":
                self._last_model = dict(item)

    def snapshot(self, limit: int = 60) -> dict:
        with self._lock:
            items = list(self._items)[-max(1, min(limit, 160)) :]
            return {"items": items, "last_model": dict(self._last_model)}


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """Protege dados com a conta Windows atual; indisponível fora do Windows."""
    if os.name != "nt":
        raise OSError("DPAPI disponível somente no Windows")
    source = ctypes.create_string_buffer(data)
    source_blob = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    target_blob = _Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(target_blob))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source_blob), "Kemy", None, None, None, 0, ctypes.byref(target_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target_blob.pbData, target_blob.cbData)
    finally:
        kernel32.LocalFree(target_blob.pbData)


class CredentialVault:
    """Cofre local ligado à conta Windows via DPAPI."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = base64.b64decode(self.path.read_bytes())
            return json.loads(_dpapi(raw, decrypt=True).decode("utf-8"))
        except Exception:
            return {}

    def update(self, values: dict[str, str]) -> None:
        with self._lock:
            data = self.load()
            data.update({str(k): str(v) for k, v in values.items() if v})
            encrypted = _dpapi(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(base64.b64encode(encrypted))
            os.replace(tmp, self.path)


def migrate_env_secrets(path: Path, secret_keys: set[str], vault: CredentialVault) -> int:
    """Move segredos de um .env legado para DPAPI sem deixar cópia em texto puro."""
    path = Path(path)
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8", errors="ignore")
    kept, secrets = [], {}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key in secret_keys and value.strip():
                secrets[key] = value.strip().strip('"').strip("'")
                kept.append(f"# {key}=<protegido no cofre do Windows>")
                continue
        kept.append(line)
    if not secrets:
        return 0
    vault.update(secrets)  # somente altera o .env depois que o cofre confirmou a gravação
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return len(secrets)


activity = ActivityTracker()


class TaskStore:
    """Fila SQLite ACID para tarefas retomáveis após falha ou reinicialização."""

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        try:
            with self._connect() as conn:
                conn.execute("create table if not exists tasks(id text primary key, prompt text, "
                             "folder text, plan text, step integer default 0, status text, "
                             "created real, updated real, result text)")
        except Exception:
            pass

    @contextmanager
    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("pragma journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add(self, tid: str, prompt: str, folder: str) -> None:
        try:
            now = time.time()
            with self._connect() as conn:
                conn.execute("insert or replace into tasks(id,prompt,folder,plan,step,status,created,updated,result)"
                             " values(?,?,?,?,?,?,?,?,?)", (tid, prompt, folder, "[]", 0, "running", now, now, ""))
        except Exception:
            pass

    def set_plan(self, tid: str, plan: list) -> None:
        try:
            with self._connect() as conn:
                conn.execute("update tasks set plan=?,updated=? where id=?", (json.dumps(plan), time.time(), tid))
        except Exception:
            pass

    def set_step(self, tid: str, step: int) -> None:
        try:
            with self._connect() as conn:
                conn.execute("update tasks set step=?,updated=? where id=?", (step, time.time(), tid))
        except Exception:
            pass

    def finish(self, tid: str, status: str, result: str = "") -> None:
        try:
            with self._connect() as conn:
                conn.execute("update tasks set status=?,result=?,updated=? where id=?",
                             (status, (result or "")[:2000], time.time(), tid))
        except Exception:
            pass

    def unfinished(self) -> list:
        try:
            with self._connect() as conn:
                rows = conn.execute("select id,prompt,folder,plan,step from tasks where status='running' "
                                    "order by updated desc").fetchall()
            return [{"id": row[0], "prompt": row[1], "folder": row[2],
                     "plan": json.loads(row[3] or "[]"), "step": row[4]} for row in rows]
        except Exception:
            return []
