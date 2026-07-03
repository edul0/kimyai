"""Cota diaria POR USUARIO (estilo Manus): cada login usa sua fatia justa do pool gratis.
O usuario so cadastra e ja usa — sem colar chave. Medido no Storage (Redis/Supabase-backed),
reseta a meia-noite UTC, com mensagem amigavel ao estourar. O dono (auth_user) e ilimitado."""
from __future__ import annotations

import datetime

from .config import Settings
from .storage import Storage


def _today() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _reset_msg() -> str:
    now = datetime.datetime.utcnow()
    nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    falta = nxt - now
    h = falta.seconds // 3600
    m = (falta.seconds % 3600) // 60
    return f"volta a meia-noite (UTC) — daqui ~{h}h{m:02d}min"


def usage_today(storage: Storage, owner: str | None) -> int:
    try:
        return int(storage.get_json(f"quota:{(owner or 'anon').lower()}:{_today()}", 0) or 0)
    except Exception:
        return 0


def check_and_consume(storage: Storage, owner: str | None, settings: Settings) -> dict:
    """Verifica e CONSOME 1 credito do usuario. Retorna {allowed, used, limit, message?}."""
    limit = int(getattr(settings, "daily_quota", 40) or 40)
    who = (owner or "anon").lower()
    # Dono do app (admin) nao tem limite.
    if owner and owner.lower() == (getattr(settings, "auth_user", "") or "").lower():
        return {"allowed": True, "used": 0, "limit": 0, "unlimited": True}
    if limit <= 0:   # 0 = desativa a cota (ilimitado)
        return {"allowed": True, "used": 0, "limit": 0, "unlimited": True}
    key = f"quota:{who}:{_today()}"
    used = 0
    try:
        used = int(storage.get_json(key, 0) or 0)
    except Exception:
        used = 0
    if used >= limit:
        return {
            "allowed": False,
            "used": used,
            "limit": limit,
            "message": (f"Voce usou suas {limit} mensagens gratis de hoje 😊. Sua cota {_reset_msg()}. "
                        "Cada usuario tem uma fatia diaria do pool gratis pra ser justo com todo mundo."),
        }
    try:
        storage.set_json(key, used + 1, ttl=60 * 60 * 36)   # ~36h, expira sozinho
    except Exception:
        pass
    return {"allowed": True, "used": used + 1, "limit": limit}
