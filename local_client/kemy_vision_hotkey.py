from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time

import mss
import mss.tools
import requests


def _capture_screen_png_bytes() -> bytes:
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        shot = sct.grab(monitor)
        return mss.tools.to_png(shot.rgb, shot.size)


def _to_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _login(session: requests.Session, base_url: str, username: str, password: str) -> None:
    url = f"{base_url.rstrip('/')}/api/auth/login"
    response = session.post(url, json={"email": username, "password": password}, timeout=20)
    response.raise_for_status()


def _analyze_screen(
    session: requests.Session,
    base_url: str,
    prompt: str,
    mode: str,
    context: str,
    session_id: str | None,
) -> dict:
    image_bytes = _capture_screen_png_bytes()
    payload = {
        "image_base64": _to_data_url(image_bytes),
        "pergunta": prompt,
        "contexto": context,
        "modo": mode,
        "session_id": session_id,
    }
    url = f"{base_url.rstrip('/')}/api/vision/analyze"
    response = session.post(url, json=payload, timeout=80)
    response.raise_for_status()
    return response.json()


def _safe_print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _run_hotkey_loop(
    session: requests.Session,
    base_url: str,
    mode: str,
    context: str,
    session_id: str | None,
    hotkey: str,
) -> None:
    try:
        import keyboard  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "Biblioteca 'keyboard' nao disponivel. Instale com: pip install keyboard"
        ) from exc

    print(f"[Kemy Vision] Atalho ativo: {hotkey}. Pressione ESC para sair.")

    def _trigger() -> None:
        try:
            result = _analyze_screen(
                session=session,
                base_url=base_url,
                prompt="Olhe esta tela e diga o que esta acontecendo de forma objetiva.",
                mode=mode,
                context=context,
                session_id=session_id,
            )
            print("\n--- RESPOSTA KEMY ---")
            print(result.get("resposta", "").strip() or result.get("resumo", "Sem resposta."))
            print("---------------------\n")
        except Exception as exc:  # pragma: no cover
            print(f"[Kemy Vision] Erro no trigger: {exc}")

    keyboard.add_hotkey(hotkey, _trigger)
    keyboard.wait("esc")


def main() -> int:
    parser = argparse.ArgumentParser(description="Kemy Vision sob demanda (F12 + screenshot).")
    parser.add_argument("--base-url", required=True, help="URL da Kemy (ex.: https://kimyai.onrender.com)")
    parser.add_argument("--user", required=True, help="Usuario/email para login na Kemy")
    parser.add_argument("--password", required=True, help="Senha para login na Kemy")
    parser.add_argument("--prompt", default="Olhe esta tela e me ajude com o que esta acontecendo.")
    parser.add_argument("--mode", default="planejamento", choices=["planejamento", "coding", "site", "auditoria"])
    parser.add_argument("--context", default="", help="Contexto fixo enviado junto com cada captura.")
    parser.add_argument("--session-id", default=None, help="Sessao opcional para vincular historico.")
    parser.add_argument("--hotkey", default="f12", help="Atalho para captura no modo loop.")
    parser.add_argument("--loop", action="store_true", help="Ativa loop por atalho (F12).")
    args = parser.parse_args()

    http = requests.Session()
    _login(http, args.base_url, args.user, args.password)
    print("[Kemy Vision] Login OK.")

    if args.loop:
        _run_hotkey_loop(
            session=http,
            base_url=args.base_url,
            mode=args.mode,
            context=args.context,
            session_id=args.session_id,
            hotkey=args.hotkey,
        )
        return 0

    started = time.perf_counter()
    result = _analyze_screen(
        session=http,
        base_url=args.base_url,
        prompt=args.prompt,
        mode=args.mode,
        context=args.context,
        session_id=args.session_id,
    )
    elapsed = (time.perf_counter() - started) * 1000
    print(f"[Kemy Vision] Analise concluida em {elapsed:.0f} ms")
    _safe_print_json(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
