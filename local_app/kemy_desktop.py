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
import base64
import datetime
import random
import threading
import time
import urllib.error
import urllib.parse
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

# User-Agent de navegador para chamadas de API (evita bloqueio 403/1010 do Cloudflare).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

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
    "skin": "#ece1f2",
    "skin_shadow": "#d9c7e6",
    "hair": "#211433",
    "hair_dark": "#120a1f",
    "hair_edge": "#3a2358",
    "eye_white": "#f3ecff",
    "iris": "#a78bfa",
    "blush": "#b06a8f",
    "lips": "#6e2440",
    "mouth": "#6e2440",
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


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\U00002190-\U000021FF\U00002300-\U000023FF"
    "\U0000FE00-\U0000FE0F\U0000200D\U00002022\U000025AA-\U000025FF]+")


def strip_emojis(text: str) -> str:
    """Remove emojis e simbolos decorativos pra UI ficar limpa/sobria. Preserva o texto."""
    if not text:
        return text
    out = _EMOJI_RE.sub("", text)
    # tira espacos duplos/sobras deixados pelos emojis removidos
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n[ \t]+", "\n", out)
    return out.strip()


def proc_quiet(**extra) -> dict:
    """kwargs pra subprocess que NAO abre janela de console nem trava num app --windowed
    (sem console): redireciona stdin e usa CREATE_NO_WINDOW no Windows."""
    kw: dict = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    kw.update(extra)
    return kw


def cleanup_update_leftovers() -> None:
    """Remove pastas residuais de updates (kemy_old_* / kemy_new_*) ao lado do app."""
    try:
        parent = EXE_DIR.parent
        for p in parent.glob("kemy_old_*"):
            shutil.rmtree(p, ignore_errors=True)
        for p in parent.glob("kemy_new_*"):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def memoria_file() -> Path:
    return config_dir() / "kemy_memoria.json"


def load_memorias() -> list:
    """Memoria persistente: licoes/preferencias que a Kemy aprendeu com o usuario."""
    try:
        d = json.loads(memoria_file().read_text(encoding="utf-8"))
        return [str(x) for x in d] if isinstance(d, list) else []
    except Exception:
        return []


def save_memorias(mems: list) -> None:
    try:
        memoria_file().write_text(json.dumps(mems[-80:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def conhecimento_file() -> Path:
    return config_dir() / "kemy_conhecimento.json"


def load_conhecimento() -> list:
    """Base de conhecimento (RAG): fatos atomicos verificados, com fonte/data/confianca."""
    try:
        d = json.loads(conhecimento_file().read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_conhecimento(facts: list) -> None:
    try:
        conhecimento_file().write_text(json.dumps(facts[-600:], ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def build_tag() -> str:
    """Identificador curto da build (sha) para sabermos qual versao esta rodando."""
    try:
        sha = (ROOT_DIR / "kemy_version.txt").read_text(encoding="utf-8").strip()
        return sha[:7] if sha else "dev"
    except Exception:
        return "dev"


def unblock_bundle() -> None:
    """Remove o 'Mark of the Web' (Zone.Identifier) das DLLs do bundle.

    Quando o zip e baixado da internet, o Windows marca os arquivos como bloqueados.
    O .NET entao se recusa a carregar a Python.Runtime.dll (pythonnet), e a UI
    moderna (webview/winforms) cai para o modo classico. Limpar o ADS resolve sem
    o usuario precisar desbloquear manualmente."""
    if os.name != "nt":
        return
    try:
        for dll in ROOT_DIR.rglob("*.dll"):
            try:
                os.remove(f"{dll}:Zone.Identifier")
            except OSError:
                pass
    except Exception:
        pass


# Motivo da falha da UI moderna (webview), exibido no modo classico para diagnostico.
WEBVIEW_ERROR = ""


def _record_webview_error(reason: str) -> None:
    global WEBVIEW_ERROR
    WEBVIEW_ERROR = reason
    try:
        (config_dir() / "webview_error.log").write_text(reason, encoding="utf-8")
    except Exception:
        pass


def find_env_file() -> Path | None:
    names = (".env", "kemy.env", "kemy_bundled.env")
    dirs = (EXE_DIR, config_dir(), ROOT_DIR, Path.cwd())
    for d in dirs:
        for nm in names:
            cand = d / nm
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


def load_merged_env() -> dict[str, str]:
    """Junta TODOS os .env: o embutido (chaves da IA) como base e o do usuario
    (config_dir, com voz/workspace) por cima. Evita que o .env do usuario apague
    as chaves da IA embutidas (bug do 'modo online / 404')."""
    merged: dict[str, str] = {}
    # Embutidos primeiro (base), depois os do usuario por cima (sobrescrevem).
    # Aceita .env, kemy.env e kemy_bundled.env em qualquer pasta conhecida.
    cands = [ROOT_DIR / "kemy_bundled.env"]
    for d in (ROOT_DIR, EXE_DIR, Path.cwd(), config_dir()):
        for nm in (".env", "kemy.env"):
            cands.append(d / nm)
    for cand in cands:
        try:
            if cand.is_file():
                merged.update(parse_env_file(cand))
        except Exception:
            continue
    return merged


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


def download_to(url: str, dest: Path, timeout: int = 90) -> bool:
    """Baixa um binario (imagem) para o disco, com 2 tentativas. True se ok."""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KemyDesktop"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if len(data) < 800:
                raise RuntimeError("vazio")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    return False


def extract_thumb_requests(text: str) -> list[dict]:
    """Le blocos ```kemy-thumb (uma por linha: 'TITULO | cena em ingles | arquivo.png')."""
    reqs: list[dict] = []
    if not text:
        return reqs
    for match in re.finditer(r"```kemy-thumb\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE):
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            title = parts[0]
            scene = parts[1] if len(parts) > 1 else ""
            fname = parts[2] if len(parts) > 2 and parts[2] else "thumbnail.png"
            style = (parts[3].lower() if len(parts) > 3 and parts[3] else "anime")
            if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fname += ".png"
            reqs.append({"title": title, "scene": scene, "file": fname, "style": style})
    return reqs


def extract_graphic_requests(text: str) -> list[dict]:
    """Le blocos ```kemy-graphic (TEXTO NITIDO em qualquer formato).
    Por linha: 'TITULO | subtitulo | arte em INGLES | arquivo.png | estilo | formato'.
    estilo: anime|gamer|neon|modern|editorial . formato: post|story|banner|poster|thumb|wide."""
    reqs: list[dict] = []
    if not text:
        return reqs
    for match in re.finditer(r"```kemy-(?:graphic|design|arte)\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE):
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if not parts[0]:
                continue
            title = parts[0]
            subtitle = parts[1] if len(parts) > 1 else ""
            scene = parts[2] if len(parts) > 2 else ""
            fname = parts[3] if len(parts) > 3 and parts[3] else f"arte{len(reqs)+1}.png"
            style = (parts[4].lower() if len(parts) > 4 and parts[4] else "modern")
            fmt = (parts[5].lower() if len(parts) > 5 and parts[5] else "post")
            if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fname += ".png"
            reqs.append({"title": title, "subtitle": subtitle, "scene": scene,
                         "file": fname, "style": style, "fmt": fmt})
    return reqs


def extract_image_requests(text: str) -> list[dict]:
    """Le blocos ```kemy-image (uma imagem por linha: 'descricao | arquivo.png | LARGxALT')."""
    reqs: list[dict] = []
    if not text:
        return reqs
    for match in re.finditer(r"```kemy-image\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE):
        for line in match.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if not parts[0]:
                continue
            fname = parts[1] if len(parts) > 1 and parts[1] else f"imagem{len(reqs)+1}.png"
            if not fname.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                fname += ".png"
            size = parts[2] if len(parts) > 2 and parts[2] else "1024x1024"
            reqs.append({"prompt": parts[0], "file": fname, "size": size})
    return reqs


# Chave do Gemini para usar o Nano Banana (Gemini 2.5 Flash Image) como gerador principal.
# Definida no boot (WebApi). Nano Banana e GRATIS (~500 imgs/dia) e MUITO melhor que o Flux,
# inclusive renderizando TEXTO legivel e mantendo personagem consistente.
GEMINI_IMAGE_KEY = ""
NANO_BANANA_MODELS = ["gemini-2.5-flash-image", "gemini-2.5-flash-image-preview"]


def gemini_image(prompt: str, dest: Path, key: str, ref_b64: str | None = None) -> bool:
    """Gera (ou edita, com ref_b64) uma imagem com o Nano Banana (Gemini Image). Salva PNG."""
    if not key:
        return False
    parts: list = [{"text": prompt[:1800]}]
    if ref_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": ref_b64}})
    payload = {"contents": [{"role": "user", "parts": parts}]}
    body = json.dumps(payload).encode("utf-8")
    for model in NANO_BANANA_MODELS:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}")
        try:
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for cand in data.get("candidates", []):
                for part in cand.get("content", {}).get("parts", []):
                    inl = part.get("inline_data") or part.get("inlineData")
                    if inl and inl.get("data"):
                        raw = base64.b64decode(inl["data"])
                        if len(raw) > 800:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(raw)
                            return True
        except Exception:
            continue
    return False


def download_image(prompt: str, dest: Path, size: str = "1024x1024") -> bool:
    """Gera uma imagem do tema e salva em disco. Tenta o Nano Banana (Gemini, gratis, melhor
    qualidade) primeiro; cai pro Pollinations Flux (sem chave) se nao tiver chave/falhar."""
    w, h = 1024, 1024
    try:
        a, _, b = size.lower().partition("x")
        if a.strip().isdigit():
            w = max(64, min(2048, int(a.strip())))
        if b.strip().isdigit():
            h = max(64, min(2048, int(b.strip())))
    except Exception:
        pass
    # 1) Nano Banana (Gemini Image) — melhor qualidade, texto legivel, consistencia.
    if GEMINI_IMAGE_KEY:
        ar = "square" if abs(w - h) < 60 else ("portrait" if h > w else "landscape")
        gp = f"{prompt[:1600]}. High quality, {ar} composition ({w}x{h})."
        if gemini_image(gp, dest, GEMINI_IMAGE_KEY):
            return True
    # 2) Pollinations Flux (gratis, sem chave) — reserva.
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:300]) +
           f"?width={w}&height={h}&nologo=true&enhance=true&model=flux&seed={random.randint(1, 99999)}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KemyDesktop"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read()
        if len(data) < 800:  # provavel erro/HTML, nao imagem
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def _thumb_font(size: int):
    from PIL import ImageFont
    for name in ("C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/ARIALBD.TTF",
                 "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/seguibl.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.truetype("arialbd.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_to_width(draw, text: str, font, max_w: int) -> list:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


# Presets de estilo de thumbnail (fonte + CSS do titulo + estilo de arte).
THUMB_STYLES = {
    "anime": {
        "font": "Anton",
        "art": "clean anime illustration, vibrant colors, sharp lineart, dynamic expressive pose",
        "css": ("color:#fff;-webkit-text-stroke:8px #0b0b0b;paint-order:stroke fill;"
                "text-shadow:6px 0 0 #ff0066,-6px 0 0 #00e0ff,"
                "11px 8px 0 rgba(60,60,60,.55),22px 16px 0 rgba(60,60,60,.30),0 0 28px rgba(0,0,0,.45)"),
    },
    "gamer": {
        "font": "Anton",
        "art": "epic gaming scene, dramatic neon lighting, ultra vibrant, high contrast, intense action",
        "css": ("color:#ffe100;-webkit-text-stroke:9px #0a0a0a;paint-order:stroke fill;"
                "text-shadow:0 0 18px #00ff6a,5px 5px 0 #b80000,-3px 0 0 #00e0ff,"
                "14px 12px 0 rgba(0,0,0,.45)"),
    },
    "neon": {
        "font": "Anton",
        "art": "futuristic cyberpunk neon scene, glowing lights, dark moody background, high contrast",
        "css": ("color:#fff;-webkit-text-stroke:5px #06000f;paint-order:stroke fill;"
                "text-shadow:0 0 12px #00e0ff,0 0 24px #00e0ff,0 0 40px #b000ff,4px 0 0 #ff007a"),
    },
    "minimal": {
        "font": "Montserrat",
        "art": "clean minimal background, soft lighting, lots of negative space, elegant, modern",
        "css": ("color:#fff;font-weight:900;-webkit-text-stroke:0;"
                "text-shadow:0 6px 22px rgba(0,0,0,.55)"),
    },
}
THUMB_STYLES["minimalista"] = THUMB_STYLES["minimal"]
THUMB_STYLES["padrao"] = THUMB_STYLES["anime"]
THUMB_STYLES["glitch"] = THUMB_STYLES["anime"]
# Estilos novos pedidos: moderno/clean (corporativo) e editorial (revista/elegante).
THUMB_STYLES["modern"] = {
    "font": "Poppins",
    "art": ("clean modern minimal composition, premium brand aesthetic, soft studio lighting, "
            "lots of negative space, subtle gradient, sophisticated"),
    "css": ("color:#ffffff;font-weight:800;-webkit-text-stroke:0;letter-spacing:-1px;"
            "text-shadow:0 4px 26px rgba(0,0,0,.45)"),
    "sub": "color:#e9ecf5;font-weight:500;text-shadow:0 2px 12px rgba(0,0,0,.5)",
}
THUMB_STYLES["clean"] = THUMB_STYLES["modern"]
THUMB_STYLES["moderno"] = THUMB_STYLES["modern"]
THUMB_STYLES["corporativo"] = THUMB_STYLES["modern"]
THUMB_STYLES["editorial"] = {
    "font": "Playfair Display",
    "art": ("elegant editorial magazine photography, sophisticated refined composition, "
            "soft natural tones, luxury feel, fine art lighting"),
    "css": ("color:#ffffff;font-weight:700;-webkit-text-stroke:0;letter-spacing:.3px;"
            "text-shadow:0 3px 20px rgba(0,0,0,.55)"),
    "sub": "color:#efeae0;font-weight:400;font-style:italic;text-shadow:0 2px 12px rgba(0,0,0,.5)",
}
THUMB_STYLES["elegante"] = THUMB_STYLES["editorial"]
THUMB_STYLES["revista"] = THUMB_STYLES["editorial"]
THUMB_FONT_IMPORT = {
    "Anton": "family=Anton",
    "Montserrat": "family=Montserrat:wght@900",
    "Poppins": "family=Poppins:wght@500;800",
    "Playfair Display": "family=Playfair+Display:ital,wght@0,700;0,900;1,400",
}

# Formatos de arte (largura x altura). Cobre YouTube, social e impressao.
GRAPHIC_SIZES = {
    "thumb": (1280, 720), "thumbnail": (1280, 720), "youtube": (1280, 720), "yt": (1280, 720),
    "wide": (1920, 1080), "wallpaper": (1920, 1080), "16:9": (1920, 1080),
    "post": (1080, 1080), "quadrado": (1080, 1080), "square": (1080, 1080), "feed": (1080, 1080),
    "story": (1080, 1920), "stories": (1080, 1920), "reels": (1080, 1920), "vertical": (1080, 1920),
    "poster": (1080, 1350), "cartaz": (1080, 1350), "retrato": (1080, 1350),
    "banner": (1500, 500), "capa": (1500, 500), "cover": (1500, 500),
}


def make_thumbnail(title: str, scene: str, dest: Path, style: str = "anime") -> bool:
    """Thumbnail profissional: arte Flux + texto composto em HTML/CSS (fontes Google + efeitos)
    renderizado em PNG pelo navegador headless. Cai para o PIL se nao tiver navegador."""
    preset = THUMB_STYLES.get((style or "anime").lower(), THUMB_STYLES["anime"])
    prompt = ((scene or "").strip() or "anime character") + ", " + preset["art"] + (
        ", character/subject placed on the RIGHT side of the frame, the LEFT side simple and "
        "uncluttered with empty space for a title, youtube thumbnail, high quality")
    tmp = dest.parent / ("_bg_" + dest.name)
    if not download_image(prompt, tmp, "1280x720"):
        return False
    ok = False
    try:
        art_b64 = base64.b64encode(tmp.read_bytes()).decode("ascii")
        if art_b64:
            ok = _thumb_html(title, art_b64, dest, preset)
    except Exception:
        ok = False
    if not ok:
        ok = _thumb_pil(title, tmp, dest)
    try:
        tmp.unlink()
    except Exception:
        pass
    return ok


def _thumb_html(title: str, art_b64: str, dest: Path, preset: dict) -> bool:
    """Compoe a thumbnail em HTML/CSS (fonte + efeitos do preset) e renderiza em PNG
    via navegador headless. Qualidade nivel designer."""
    t = (title or "").strip().upper().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    font = preset.get("font", "Anton")
    font_import = THUMB_FONT_IMPORT.get(font, "family=Anton")
    title_css = preset.get("css", "")
    html = """<!doctype html><html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?__FONTIMPORT__&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1280px;height:720px;overflow:hidden;background:#000}
.thumb{width:1280px;height:720px;position:relative;font-family:'__FONT__',Impact,sans-serif;
  background:url('data:image/jpeg;base64,__ART__') center/cover no-repeat}
.shade{position:absolute;inset:0;
  background:linear-gradient(90deg, rgba(4,4,10,.82) 0%, rgba(4,4,10,.55) 34%, rgba(4,4,10,0) 58%)}
.wrap{position:absolute;left:54px;top:0;height:720px;width:610px;display:flex;align-items:center}
.title{text-transform:uppercase;line-height:.96;letter-spacing:1px;font-weight:400;__TITLECSS__}
</style></head><body>
<div class="thumb"><div class="shade"></div>
<div class="wrap"><div class="title" id="t">__TITLE__</div></div></div>
<script>
var el=document.getElementById('t'),box=el.parentElement,s=180;
function fit(){el.style.fontSize=s+'px';
  while((el.scrollHeight>box.clientHeight-20||el.scrollWidth>box.clientWidth-6)&&s>40){s-=4;el.style.fontSize=s+'px';}}
if(document.fonts&&document.fonts.ready){document.fonts.ready.then(fit);setTimeout(fit,1500);}else{fit();}
</script></body></html>"""
    html = (html.replace("__FONTIMPORT__", font_import).replace("__FONT__", font)
            .replace("__TITLECSS__", title_css).replace("__ART__", art_b64).replace("__TITLE__", t))
    hpath = dest.parent / ("_thumb_" + dest.stem + ".html")
    try:
        hpath.write_text(html, encoding="utf-8")
    except Exception:
        return False
    try:
        ok = render_html_to_png(hpath, dest, 1280, 720)
    finally:
        try:
            hpath.unlink()
        except Exception:
            pass
    return ok


def make_graphic(title: str, subtitle: str, scene: str, dest: Path,
                 style: str = "modern", fmt: str = "post") -> bool:
    """Arte profissional com TEXTO NITIDO em qualquer formato (post, story, banner, poster,
    thumb): gera a arte no Flux SEM TEXTO e escreve titulo/subtitulo por cima em HTML/CSS,
    renderizado em PNG pelo navegador headless. Resolve o 'texto lixoso' do gerador."""
    preset = THUMB_STYLES.get((style or "modern").lower(), THUMB_STYLES["modern"])
    w, h = GRAPHIC_SIZES.get((fmt or "post").lower(), (1080, 1080))
    portrait = h >= w
    side = ("composition with the main subject kept to one side and clear empty space "
            "for a title" if not portrait else
            "composition with clear empty space at the top and bottom for a title")
    prompt = ((scene or "").strip() or "abstract premium background") + ", " + preset["art"] + (
        f", {side}, NO TEXT, no letters, no words, no typography, no watermark, high quality")
    tmp = dest.parent / ("_bg_" + dest.name)
    if not download_image(prompt, tmp, f"{w}x{h}"):
        return False
    ok = False
    try:
        art_b64 = base64.b64encode(tmp.read_bytes()).decode("ascii")
        if art_b64:
            ok = _graphic_html(title, subtitle, art_b64, dest, preset, w, h)
    except Exception:
        ok = False
    if not ok:
        ok = _thumb_pil(title, tmp, dest)
    try:
        tmp.unlink()
    except Exception:
        pass
    return ok


def _graphic_html(title: str, subtitle: str, art_b64: str, dest: Path, preset: dict,
                  w: int, h: int) -> bool:
    """Compoe titulo + subtitulo sobre a arte, com layout adaptado ao formato (paisagem =
    texto a esquerda; quadrado/retrato = texto embaixo), e renderiza em PNG."""
    def esc(s):
        return (s or "").strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    portrait = h >= w
    t = esc(title)
    sub = esc(subtitle)
    font = preset.get("font", "Poppins")
    font_import = THUMB_FONT_IMPORT.get(font, "family=Poppins:wght@500;800")
    title_css = preset.get("css", "")
    sub_css = preset.get("sub", "color:#fff;font-weight:600;text-shadow:0 2px 12px rgba(0,0,0,.5)")
    if portrait:
        shade = ("linear-gradient(180deg, rgba(4,4,10,.62) 0%, rgba(4,4,10,0) 30%,"
                 " rgba(4,4,10,0) 52%, rgba(4,4,10,.86) 100%)")
        wrap = (f"position:absolute;left:6%;right:6%;bottom:6%;display:flex;flex-direction:column;"
                f"gap:{max(10,h//70)}px;align-items:flex-start;text-align:left")
        base_fs = int(w * 0.13)
        box_w = int(w * 0.88)
        box_h = int(h * 0.42)
    else:
        shade = ("linear-gradient(90deg, rgba(4,4,10,.84) 0%, rgba(4,4,10,.55) 36%,"
                 " rgba(4,4,10,0) 62%)")
        wrap = (f"position:absolute;left:5%;top:0;height:{h}px;width:52%;display:flex;"
                f"flex-direction:column;justify-content:center;gap:{max(10,h//50)}px;text-align:left")
        base_fs = int(h * 0.22)
        box_w = int(w * 0.52) - 20
        box_h = int(h * 0.8)
    sub_html = f'<div class="sub" id="s">{sub}</div>' if sub else ""
    html = """<!doctype html><html><head><meta charset="utf-8">
<style>
@import url('https://fonts.googleapis.com/css2?__FI__&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:__W__px;height:__H__px;overflow:hidden;background:#000}
.c{width:__W__px;height:__H__px;position:relative;font-family:'__FONT__',Impact,sans-serif;
  background:url('data:image/jpeg;base64,__ART__') center/cover no-repeat}
.shade{position:absolute;inset:0;background:__SHADE__}
.wrap{__WRAP__}
.title{text-transform:none;line-height:1.0;__TCSS__}
.sub{line-height:1.2;font-size:__SUBFS__px;__SCSS__}
</style></head><body>
<div class="c"><div class="shade"></div>
<div class="wrap"><div class="title" id="t">__TITLE__</div>__SUBHTML__</div></div>
<script>
var el=document.getElementById('t'),s=__FS__;
function fit(){el.style.fontSize=s+'px';
  while((el.scrollHeight>__BH__||el.scrollWidth>__BW__)&&s>22){s-=4;el.style.fontSize=s+'px';}}
if(document.fonts&&document.fonts.ready){document.fonts.ready.then(fit);setTimeout(fit,1500);}else{fit();}
</script></body></html>"""
    repl = {
        "__FI__": font_import, "__W__": str(w), "__H__": str(h), "__FONT__": font,
        "__ART__": art_b64, "__SHADE__": shade, "__WRAP__": wrap, "__TCSS__": title_css,
        "__SCSS__": sub_css, "__SUBFS__": str(max(20, base_fs // 3)),
        "__TITLE__": t, "__SUBHTML__": sub_html, "__FS__": str(base_fs),
        "__BH__": str(int(box_h * 0.72)), "__BW__": str(box_w),
    }
    for k, v in repl.items():
        html = html.replace(k, v)
    hpath = dest.parent / ("_gfx_" + dest.stem + ".html")
    try:
        hpath.write_text(html, encoding="utf-8")
    except Exception:
        return False
    try:
        ok = render_html_to_png(hpath, dest, w, h)
    finally:
        try:
            hpath.unlink()
        except Exception:
            pass
    return ok


def _thumb_pil(title: str, tmp: Path, dest: Path) -> bool:
    """Reserva (sem navegador): desenha o titulo com PIL na esquerda."""
    from PIL import Image, ImageDraw
    try:
        img = Image.open(tmp).convert("RGBA").resize((1280, 720))
    except Exception:
        return False
    title = (title or "").strip().upper()
    if title:
        # leve escurecimento na ESQUERDA para o texto destacar (gradiente lateral)
        grad = Image.new("L", (1280, 1), 0)
        for xx in range(1280):
            grad.putpixel((xx, 0), int(150 * max(0, (640 - xx) / 640)))
        grad = grad.resize((1280, 720))
        img = Image.composite(Image.new("RGBA", img.size, (8, 8, 16, 255)), img, grad)
        d0 = ImageDraw.Draw(img)
        area_w = 600                      # area de texto na esquerda
        size, lines, font = 130, [title], None
        while size >= 44:
            font = _thumb_font(size)
            lines = _wrap_to_width(d0, title, font, area_w)
            if int(size * 1.04) * len(lines) <= 560:
                break
            size -= 6
        line_h = int(size * 1.04)
        total_h = line_h * len(lines)
        y0 = (720 - total_h) // 2          # centralizado verticalmente
        x_left = 52
        stroke = max(7, size // 9)

        def _layer(dx, dy, fill, alpha=255):
            lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            dd = ImageDraw.Draw(lay)
            yy = y0 + dy
            for ln in lines:
                dd.text((x_left + dx, yy), ln, font=font, fill=fill + (alpha,))
                yy += line_h
            return lay

        for off, a in ((26, 45), (16, 70), (9, 100)):
            img.alpha_composite(_layer(-off, -off // 2, (50, 50, 50), a))
        img.alpha_composite(_layer(-6, 0, (0, 220, 255), 170))
        img.alpha_composite(_layer(6, 0, (255, 0, 110), 170))
        draw = ImageDraw.Draw(img)
        yy = y0
        for ln in lines:
            draw.text((x_left, yy), ln, font=font, fill="#FFFFFF",
                      stroke_width=stroke, stroke_fill=(8, 8, 8))
            yy += line_h
    img = img.convert("RGB")
    try:
        img.save(dest, quality=94)
        return True
    except Exception:
        return False


def _find_browser() -> str | None:
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if Path(c).exists():
            return c
    return None


def html_to_pdf(html_path: Path, pdf_path: Path) -> bool:
    """Converte um HTML em PDF de verdade usando o Edge/Chrome headless (sem libs extras)."""
    browser = _find_browser()
    if not browser:
        return False
    try:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                       timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not pdf_path.exists():
            subprocess.run([browser, "--headless", "--disable-gpu",
                            f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                           timeout=90, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return pdf_path.exists()
    except Exception:
        return False


def render_html_to_png(html_path: Path, png_path: Path, w: int = 1280, h: int = 720) -> bool:
    """Renderiza um HTML em PNG via Edge/Chrome headless (captura na resolucao dada)."""
    browser = _find_browser()
    if not browser:
        return False
    base = [browser, "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
            "--default-background-color=00000000", f"--window-size={w},{h}",
            "--virtual-time-budget=3000", f"--screenshot={png_path}", html_path.as_uri()]
    try:
        subprocess.run([browser, "--headless=new"] + base[1:], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not png_path.exists():
            subprocess.run([browser, "--headless"] + base[1:], timeout=60,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return png_path.exists()
    except Exception:
        return False


def fetch_url_text(url: str, limit: int = 4000) -> str:
    """Baixa uma pagina e devolve o texto limpo (sem tags), truncado."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KemyDesktop"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(2_000_000)
        html = raw.decode("utf-8", "ignore")
        html = re.sub(r"(?is)<(script|style|noscript|svg).*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        text = re.sub(r"&[a-z#0-9]+;", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception:
        return ""


def web_search(query: str, limit: int = 6) -> str:
    """Busca best-effort no DuckDuckGo (HTML), devolve titulos + links."""
    try:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KemyDesktop"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", "ignore")
        out: list[str] = []
        for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL):
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            link = m.group(1)
            dec = re.search(r"uddg=([^&]+)", link)
            if dec:
                link = urllib.parse.unquote(dec.group(1))
            if title:
                out.append(f"- {title} ({link})")
            if len(out) >= limit:
                break
        return "\n".join(out)
    except Exception:
        return ""


def youtube_first_video(query: str):
    """Acha o ID do primeiro video do YouTube para a busca (pra tocar com autoplay).

    Tenta varios padroes/endpoints pra quase nunca cair na busca crua.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    }
    q = urllib.parse.quote(query)
    # &sp=EgIQAQ%3D%3D filtra so videos (evita pegar canal/playlist como 1o resultado).
    urls = [
        "https://www.youtube.com/results?search_query=" + q + "&sp=EgIQAQ%3D%3D",
        "https://www.youtube.com/results?search_query=" + q,
        "https://m.youtube.com/results?search_query=" + q,
    ]
    patterns = [
        r'"videoRenderer":\{"videoId":"([\w-]{11})"',
        r'"videoId":"([\w-]{11})"',
        r'watch\?v=([\w-]{11})',
        r'"url":"/watch\?v=([\w-]{11})',
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
            for pat in patterns:
                m = re.search(pat, html)
                if m:
                    return m.group(1)
        except Exception:
            continue
    return None


def web_image_search(query: str, limit: int = 3) -> list:
    """Busca imagens de referencia no DuckDuckGo (best-effort). Retorna URLs de imagens."""
    try:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        q = urllib.parse.quote(query)
        req = urllib.request.Request("https://duckduckgo.com/?q=" + q + "&iax=images&ia=images", headers=h)
        page = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        m = re.search(r'vqd=["\']?([\w.-]+)', page)
        if not m:
            return []
        vqd = m.group(1)
        url = f"https://duckduckgo.com/i.js?l=us-en&o=json&q={q}&vqd={vqd}&f=,,,&p=1"
        req2 = urllib.request.Request(url, headers={**h, "Referer": "https://duckduckgo.com/"})
        data = json.loads(urllib.request.urlopen(req2, timeout=15).read().decode("utf-8", "ignore"))
        return [it["image"] for it in (data.get("results") or [])[:limit] if it.get("image")]
    except Exception:
        return []


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
    "ESTRATEGIA (pense ANTES de codar, como um engenheiro senior):\n"
    "- Entenda o OBJETIVO REAL do pedido e mire em entregar 100% dele, completo e funcionando.\n"
    "- Escolha o MELHOR METODO para o caso, nao o mais preguicoso:\n"
    "  * Muitos dados/itens reais (Pokedex, catalogo, filmes, cripto, clima...) -> busque de uma "
    "API publica GRATUITA via fetch no JS e renderize tudo dinamicamente (busca/paginacao). "
    "Pense em qual API encaixa: PokeAPI, TheMealDB, CoinGecko, OpenWeather, REST Countries, etc.\n"
    "  * Recurso pronto (graficos, mapas, animacao, slider, datas) -> use uma biblioteca/CDN "
    "consagrada em vez de reinventar.\n"
    "  * Coisa simples -> codigo proprio enxuto, sem dependencia a toa.\n"
    "- NUNCA entregue versao 'exemplo', limitada ou pela metade. Se o pedido implica muitos itens, "
    "traga TODOS; se precisa de dado real, busque da fonte; se precisa funcionar, garanta que funciona.\n"
    "- Em caso de duvida sobre a melhor abordagem/fonte, escolha a solucao que um profissional usaria "
    "e que entrega o resultado mais completo e bonito.\n"
    "Regras:\n"
    "1) Responda SEMPRE em portugues, curto e direto, como uma colega de equipe.\n"
    "2) Quando criar ou alterar arquivos, devolva CADA arquivo COMPLETO no formato "
    "EXATO (nada de '...'):\n"
    "<<<FILE: caminho/do/arquivo>>>\n"
    "conteudo completo do arquivo\n"
    "<<<END>>>\n"
    "   FORMATO OBRIGATORIO. PROIBIDO usar '**index.html**', '### index.html', titulos em "
    "markdown ou colocar so o nome do arquivo. SEMPRE comece com '<<<FILE: nome>>>' numa linha, "
    "depois o codigo, depois '<<<END>>>'. NUNCA escreva '<<<END>>>' sem ter aberto '<<<FILE:>>>' "
    "antes, e NUNCA deixe um arquivo vazio — todo arquivo deve ter o codigo completo dentro.\n"
    "3) Ao ATUALIZAR um projeto existente: PRESERVE o design, o layout e tudo que ja "
    "funciona — mude APENAS o que foi pedido. Use os ARQUIVOS ATUAIS como base e reenvie "
    "completos SO os arquivos que mudaram (os outros nao precisa reenviar). NUNCA simplifique, "
    "remova recursos ou quebre o visual que ja estava bom. Nao recomece do zero.\n"
    "3b) FUNCIONALIDADE REAL — proibido enrolar: NUNCA use placeholders como 'conteudo seria "
    "carregado aqui', 'TODO', funcao vazia ou botao sem acao. Cada botao/menu deve FAZER algo de "
    "verdade (abrir a secao, salvar no localStorage, filtrar uma tabela, abrir um modal funcional, "
    "etc.). Se for muita coisa, implemente o nucleo funcionando de verdade em vez de varios "
    "placeholders.\n"
    "3c) PROJETO GRANDE (ERP, dashboard, app com varios modulos): DIVIDA em VARIOS arquivos "
    "pequenos (ex.: um .js por modulo: cadastros.js, financeiro.js, estoque.js...) em vez de um "
    "script.js gigante. Assim cada arquivo cabe na resposta sem truncar e da pra editar um modulo "
    "sem reescrever o app inteiro. Carregue-os no index.html com varios <script src=...>.\n"
    "3d) EDICAO CIRURGICA (raciocine como um dev: leia os ARQUIVOS ATUAIS e mude SO o necessario). "
    "Para mexer num arquivo que JA EXISTE, NAO reenvie o arquivo inteiro — mande so o trecho que muda, "
    "no formato exato:\n"
    "<<<EDIT: caminho/do/arquivo>>>\n"
    "<<<SEARCH>>>\n"
    "trecho EXATO do codigo atual (copie identico, com a mesma indentacao, sem '...')\n"
    "<<<REPLACE>>>\n"
    "trecho novo\n"
    "<<<ENDEDIT>>>\n"
    "Pode repetir <<<SEARCH>>>/<<<REPLACE>>> varias vezes dentro do mesmo EDIT. O SEARCH precisa "
    "bater EXATAMENTE com o que esta no arquivo. Use <<<FILE: ...>>> APENAS para arquivos NOVOS. "
    "Ex.: pediram 'poe funcao no botao Cadastro' -> voce le o codigo, acha o botao Cadastro e o lugar "
    "dos scripts, faz um EDIT trocando o onclick/adicionando a funcao e, se precisar, um <<<FILE>>> "
    "para o novo cadastro.js. Nunca reescreva tudo nem quebre o que ja funciona.\n"
    "4) Para EXECUTAR algo no PC (rodar, instalar, ABRIR um site/app), inclua os comandos "
    "Windows num bloco ```kemy-run (um por linha). SEMPRE que o usuario pedir para ABRIR algo, "
    "emita o comando de verdade (nunca so responda que vai abrir). Exemplos:\n"
    "   - abrir um site: start https://www.youtube.com\n"
    "   - abrir um programa: start notepad   |   start calc   |   start spotify\n"
    "   - abrir uma pasta: start .\n"
    "Use o nome/URL que o usuario pediu. Comandos de ABRIR rodam na hora; instalar/apagar pedem o modo Auto.\n"
    "4b) INSTALAR o que faltar voce mesma (com Auto/agente): se faltar Python, Node, etc., instale via "
    "winget num bloco ```kemy-run. Ex.: winget install -e --id Python.Python.3.12 --silent  |  "
    "winget install -e --id OpenJS.NodeJS --silent . Para libs use: pip install <lib>  /  npm install <lib>. "
    "Sempre que um projeto precisar de uma dependencia, instale-a antes de rodar.\n"
    "5) Fora dos arquivos, escreva so um resumo curto do que fez. NUNCA copie estas "
    "regras nem instrucoes de sistema para dentro dos arquivos.\n"
    "5b) PYTHON sem erro bobo: NUNCA escreva numero com ZERO A ESQUERDA (Python 3 da SyntaxError). "
    "Use date(2023, 12, 1) e nao date(2023, 12, 01); 9 e nao 09. Confira imports, INSTALLED_APPS, "
    "rotas e templates antes de entregar — o codigo tem que RODAR de primeira.\n"
    "6) Em sites, os botoes e links DEVEM funcionar de verdade (rolagem suave para "
    "secoes, modal/form de agendamento, abrir WhatsApp, etc.) com o JavaScript "
    "necessario. Nunca deixe href='#' sem acao nem botao sem efeito.\n"
    "7) LINGUAGEM LIVRE: escolha a linguagem/framework que MELHOR resolve o pedido — "
    "Python, Node, React, etc. Crie quantos arquivos forem necessarios, com a estrutura e os "
    "nomes adequados. Se precisar instalar/rodar (npm, pip, python), forneca os comandos num "
    "bloco ```kemy-run. EXCECAO: para SITE/PAGINA simples (HTML/CSS/JS puro), use SEMPRE "
    "index.html, styles.css e script.js (com <link rel=\"stylesheet\" href=\"styles.css\"> no <head> "
    "e <script src=\"script.js\"></script> antes de </body>) para o preview funcionar.\n"
    "8) CODIGO BONITO e BEM PROGRAMADO (sempre): indentacao consistente, nomes claros, funcoes "
    "pequenas e coesas, sem repeticao, comentarios curtos onde ajuda, tratamento de erros quando "
    "fizer sentido, e boas praticas da linguagem. Nada de codigo baguncado ou pela metade. "
    "Ao alterar um site, reenvie os arquivos afetados COMPLETOS e consistentes entre si.\n"
    "9) DESIGN nivel profissional (OBRIGATORIO, capriche muito):\n"
    "   - Importe um Google Font moderno no <head> (ex.: Poppins, Inter, Plus Jakarta Sans, Sora).\n"
    "   - Defina paleta em :root com variaveis CSS, coerente com o tema do negocio "
    "(ex.: barbearia = grafite #111 + dourado #d4af37; pet = tons quentes; etc.).\n"
    "   - Container central: max-width ~1120px, margin auto, padding lateral 24px. "
    "Secoes empilhadas com MUITO respiro (padding vertical 72-96px).\n"
    "   - HERO ocupando ~85vh: conteudo CENTRALIZADO e EMPILHADO em coluna -> eyebrow "
    "pequeno, depois H1 GRANDE (clamp 40-72px, peso 800, line-height 1.05), depois "
    "subtitulo (max 60ch), e SO ENTAO os botoes numa linha ABAIXO. NUNCA titulo, texto "
    "e botao na mesma linha. Fundo do hero com gradiente rico ou imagem com overlay escuro.\n"
    "   - Secoes obrigatorias: header fixo translucido (backdrop-filter blur) com nav; "
    "hero; servicos em GRID de cards (repeat(auto-fit,minmax(240px,1fr)) com gap 24px); "
    "uma secao sobre/galeria; depoimentos; CTA final; e footer.\n"
    "   - Cards e botoes: border-radius 14-18px, sombra suave, transition 0.2s e hover "
    "que eleva (translateY -4px) ou aumenta brilho. Botao primario preenchido + secundario "
    "contornado.\n"
    "   - Tipografia: titulos grandes e fortes, corpo com line-height 1.6, cores de texto "
    "com bom contraste (nao use cinza fraco em fundo cinza).\n"
    "   - RESPONSIVO: mobile-first com @media (max-width: 768px) ajustando grid e fontes.\n"
    "   - PROIBIDO: layout cru, fundo cinza chapado sem graca, textos colados, tudo numa "
    "linha so, ou pagina sem hierarquia visual. Entregue algo que pareca feito por um "
    "designer senior.\n"
    "10) IMAGENS sempre CONTEXTUAIS, ligadas ao tema do site. Use o gerador gratuito "
    "(sem chave) https://image.pollinations.ai/prompt/<DESCRICAO>?width=1200&height=800&nologo=true "
    "onde <DESCRICAO> descreve a imagem em INGLES e url-encoded (espaco = %20), ex.: "
    "modern%20barbershop%20interior , man%20getting%20a%20haircut , barber%20cutting%20beard . "
    "Varie &seed=1, &seed=2, &seed=3 para imagens diferentes. Ajuste width/height ao uso (card "
    "quadrado 600x600, hero largo 1600x900). NUNCA use picsum.photos nem source.unsplash.com "
    "(devolvem fotos aleatorias sem relacao com o tema).\n"
    "11) FUNDOS: toda imagem de fundo usa SEMPRE center/cover no-repeat. Hero com imagem = "
    "background: linear-gradient(rgba(0,0,0,.55),rgba(0,0,0,.55)), url('...pollinations...') center/cover; "
    "PROIBIDO background-size que repita em faixas (ex.: 100% 300px) ou gradiente cinza chapado. "
    "Imagens em <img> tambem com object-fit: cover e width/height definidos para nao distorcer.\n"
    "12) PADRAO DE QUALIDADE (emule este nivel; adapte cores/copy/imagens ao tema):\n"
    "HTML hero -> <section class=\"hero\"><div class=\"hero-in\">"
    "<span class=\"eyebrow\">BARBEARIA PREMIUM</span>"
    "<h1>Seu estilo, no capricho</h1>"
    "<p>Cortes e barba com hora marcada, sem fila.</p>"
    "<div class=\"cta\"><a href=\"#agendar\" class=\"btn primary\">Agendar agora</a>"
    "<a href=\"#servicos\" class=\"btn ghost\">Ver servicos</a></div></div></section>\n"
    "CSS -> :root{--bg:#0e0e10;--card:#17171b;--gold:#d4af37;--text:#f3f3f5;--muted:#a7a7ad}\n"
    ".hero{min-height:85vh;display:grid;place-items:center;text-align:center;color:#fff;"
    "background:linear-gradient(rgba(0,0,0,.62),rgba(0,0,0,.62)),"
    "url('https://image.pollinations.ai/prompt/modern%20barbershop%20interior%20cinematic?width=1600&height=900&nologo=true') center/cover}\n"
    ".hero-in{display:flex;flex-direction:column;align-items:center;gap:18px;max-width:680px;padding:0 24px}\n"
    ".eyebrow{letter-spacing:3px;font-size:13px;color:var(--gold);font-weight:700}\n"
    ".hero h1{font-size:clamp(40px,6vw,72px);font-weight:800;line-height:1.05;margin:0}\n"
    ".btn{padding:14px 26px;border-radius:14px;font-weight:700;text-decoration:none;transition:.2s}\n"
    ".btn.primary{background:var(--gold);color:#1a1304}.btn.primary:hover{transform:translateY(-3px)}\n"
    ".btn.ghost{border:1px solid rgba(255,255,255,.5);color:#fff}\n"
    "Cards de servico em grid responsivo com imagem Pollinations do servico no topo de cada card.\n"
    "13) GERAR IMAGEM AVULSA (logo, foto, icone, thumbnail) que o usuario pediu fora de um site: "
    "use um bloco ```kemy-image com UMA imagem por linha no formato "
    "'descricao em INGLES | nome-arquivo.png | LARGURAxALTURA'. "
    "Ex.: minimalist barber logo, gold on black background | logo.png | 800x800 . "
    "Para THUMBNAIL do YouTube use 1280x720 e estilo chamativo (cores fortes, contraste). "
    "VISUAL CANONICO DA KEMY (use SEMPRE que o usuario pedir 'voce'/'a Kemy'/'a IA' na imagem, "
    "para ela ficar consistente): 'cute anime vtuber girl, short dark purple bob hair with side bangs, "
    "bright blue eyes, small teal diamond gem on forehead, black business suit blazer with a light-blue "
    "scarf, friendly'. NUNCA deixe a Kemy de fora se o usuario pediu ela — descreva-a explicitamente na cena. "
    "Se o usuario pedir A PROPRIA imagem/foto, voce NAO tem o rosto real dele: peca para ele anexar uma "
    "foto pelo 📎 (a Kemy ve a imagem) e descreva uma versao baseada nela, ou avise que sera uma pessoa "
    "generica. A Kemy baixa e salva a imagem automaticamente.\n"
    "13b) THUMBNAIL do YouTube (quando falar 'thumb', 'thumbnail', 'capa' ou 'miniatura'): "
    "use um bloco ```kemy-thumb com 'TITULO | cena em INGLES | arquivo.png | ESTILO'. A Kemy compoe a "
    "thumb profissional (titulo desenhado por cima com fonte forte, contorno e efeitos) — entao NAO inclua "
    "o texto na cena, so descreva a ARTE (personagem/cena expressivo, do lado DIREITO). "
    "ESTILO (4o campo) escolha conforme o video: 'anime' (padrao, glitch+cromatico), 'gamer' (amarelo, "
    "brilho neon, intenso), 'neon' (cyberpunk brilhante), 'minimal' (limpo e elegante). "
    "Inclua o visual canonico da Kemy se ela aparecer. "
    "Ex.: REAGINDO A ISSO?! | expressive anime girl with dark twin-tails, shocked happy face, hands up | thumb.png | anime\n"
    "13c) ARTE/DESIGN GRAFICO COM TEXTO (poster, banner, capa, post de social, story, flyer, "
    "anuncio) — REGRA DE OURO: o gerador de imagem NAO sabe escrever (sai texto borrado). Entao "
    "NUNCA peca o texto dentro da arte. Use um bloco ```kemy-graphic com "
    "'TITULO | subtitulo | ARTE em INGLES (sem texto) | arquivo.png | estilo | formato'. A Kemy gera a "
    "arte limpa no Flux e escreve o TITULO/SUBTITULO por cima com tipografia nitida. "
    "estilo: anime (glitch/cromatico), gamer, neon, modern (clean/corporativo), editorial (revista/elegante). "
    "formato: post (1080x1080), story (1080x1920), banner (1500x500), poster (1080x1350), thumb (1280x720), wide (1920x1080). "
    "Descreva so a ARTE/cena/atmosfera (cores, estilo, elementos), deixando espaco pro texto. "
    "Ex.: BLACK FRIDAY | ate 70% OFF | premium shopping bags and gold confetti on dark gradient, luxury | promo.png | modern | post\n"
    "14) CONTEUDO COM MUITOS ITENS/DADOS (Pokedex, catalogo grande, lista de filmes, "
    "criptos, etc.): NUNCA escreva os dados na mao (voce trunca e fica incompleto). "
    "Em vez disso, BUSQUE de uma API publica gratuita via fetch no JavaScript e renderize "
    "DINAMICAMENTE (com busca, paginacao ou scroll infinito). Exemplos de APIs gratis e "
    "sem chave: Pokemon -> https://pokeapi.co/api/v2/pokemon?limit=151 (e a sprite em "
    "results[i].url -> sprites.front_default); filmes/series, cripto (CoinGecko), etc. "
    "Entregue a lista COMPLETA, nunca so 2-3 exemplos.\n"
    "15) DOCUMENTOS, SLIDES e PDF (voce tambem faz isso, capriche):\n"
    "  - SLIDES/APRESENTACAO -> index.html com reveal.js@5 (reveal.css + dist/reveal.js + Reveal.initialize"
    "({hash:true,transition:'slide'})). NAO use o tema cru padrao: escreva um CSS PROPRIO bonito. Padrao de "
    "qualidade: importe Google Font (Poppins/Sora/Inter); fundo escuro com gradiente "
    "(.reveal{background:radial-gradient(900px 600px at 80% -10%,#241b4d,#0c0a1a)}); cor de destaque "
    "(--accent:#7c5cff ou similar). VARIE os layouts entre os slides: (1) CAPA com titulo gigante + "
    "subtitulo + barra de destaque; (2) bullets com icones/emojis e bastante respiro; (3) DUAS COLUNAS "
    "(texto | imagem Flux); (4) slide so com numero/estatistica GIGANTE; (5) citacao; (6) slide de "
    "imagem cheia com overlay; (7) ENCERRAMENTO com CTA. Titulos grandes (clamp), texto legivel, margem "
    "generosa, transicoes suaves. Use imagens do Flux (pollinations) quando ajudar. Nada de slide cru.\n"
    "  - DOCUMENTO/RELATORIO/CURRICULO/CARTA -> gere um HTML bonito e bem formatado com CSS de "
    "impressao (@page{size:A4;margin:2cm}, fonte legivel, titulos, listas, tabelas). Se o usuario "
    "pedir PDF, a Kemy converte o HTML em PDF sozinha — voce so precisa entregar o HTML caprichado.\n"
    "  - PLANILHA -> gere um arquivo .csv (separado por virgula) ou uma tabela HTML.\n"
    "16) NIVEL DE ENGENHARIA (estilo Codex/Claude Code): aja como um agente senior — leia os arquivos, "
    "entenda o contexto, planeje a melhor abordagem, edite de forma cirurgica e garanta que o resultado "
    "RODA e fica completo. Voce e versatil: codigo, sites, apps, jogos, imagens, thumbnails, documentos, "
    "slides, automacao do PC, visao e voz — seja excelente em tudo.\n"
    "17) PROATIVA: ao TERMINAR um projeto/tarefa, no final do resumo sugira 2-3 proximos passos curtos e "
    "uteis (ex.: 'Quer que eu adicione um formulario de contato? Posso publicar o site no ar? Adiciono modo "
    "escuro?'). Seja util como um colega senior, sem encher.\n"
    "18) SISTEMA COMPLETO / ERP / PLATAFORMA / CRUD / DASHBOARD (regra CRITICA, leia com atencao): "
    "quando o pedido for um SISTEMA de verdade (ERP, plataforma, painel admin, app com login, CRUD, "
    "estoque, vendas, financeiro, agendamento, etc.), e PROIBIDO entregar um unico index.html simples ou "
    "uma tela 'feia' de exemplo. Entregue uma APLICACAO REAL, MULTI-ARQUIVO e ARQUITETADA:\n"
    "   - Respeite a LINGUAGEM/STACK que o usuario pediu. Se ele disse 'em Python' -> Flask ou FastAPI "
    "(app.py + templates/ + static/ + models + rotas + persistencia em SQLite ou JSON); 'em Node' -> "
    "Express; 'em React/Next' -> componentes reais. Se NAO especificou a stack, escolha a melhor e diga qual.\n"
    "   - SEPARE em arquivos coerentes (models, rotas/controllers, services, templates/componentes, "
    "static/css, static/js, db). Nada de tudo amontoado num arquivo so.\n"
    "   - Implemente os MODULOS de verdade, funcionando ponta a ponta: listar, criar, editar, excluir, "
    "buscar/filtrar, e PERSISTIR os dados (SQLite/JSON/arquivo). Nada de botao que nao faz nada nem "
    "'// TODO implementar'. Inclua dados de exemplo (seed) para abrir e ja ver funcionando.\n"
    "   - !!! TODO BOTAO E LINK TEM QUE FUNCIONAR DE VERDADE !!! 'Criar/Nova' abre um FORMULARIO (pagina "
    "ou modal) que ao enviar SALVA no banco e volta pra lista atualizada; 'Editar' carrega os dados no "
    "formulario e atualiza; 'Excluir' remove (com confirmacao) e some da lista. Crie as ROTAS/views/handlers "
    "e os templates de formulario de CADA modulo. Em Django: urls + views (GET form / POST salva) + "
    "ModelForm + template do form + redirect. PROIBIDO entregar tabela so-leitura ou botao decorativo: se um "
    "modulo aparece no menu, o seu CRUD inteiro funciona. Teste mentalmente cada botao antes de entregar.\n"
    "   - UI de PAINEL profissional: sidebar de navegacao entre modulos, topbar, area de conteudo com "
    "tabelas/cards, formularios em modal ou pagina, estados de vazio/carregando, e o MESMO nivel de capricho "
    "visual da regra 9 (fonte boa, paleta coerente, espacamento, responsivo). Um ERP deve PARECER um ERP.\n"
    "   - Forneca os comandos para instalar e rodar num bloco ```kemy-run (ex.: pip install flask; "
    "python app.py) e um README curto com como usar.\n"
    "   - Se for grande demais para uma resposta, ENTREGUE O ESQUELETO COMPLETO E FUNCIONAL (todos os "
    "arquivos, rodando, com 1-2 modulos prontos de exemplo) e diga claramente o que falta — NUNCA um stub "
    "vazio e feio. O criterio e: o usuario abre, roda o comando, e ja tem um sistema utilizavel na cara dele.\n"
    "19) BANCO DE DADOS / PERSISTENCIA (sempre que houver dados a guardar — cadastros, vendas, estoque, "
    "usuarios, etc.): NUNCA deixe os dados so na memoria/variavel (somem ao recarregar). Use um banco DE "
    "VERDADE:\n"
    "   - PADRAO = SQLite LOCAL (zero configuracao, sem chave, funciona offline). Django -> ja usa SQLite "
    "(models + migrate). Flask/FastAPI -> sqlite3 ou SQLAlchemy criando o arquivo .db e as tabelas no 1o run. "
    "Node -> better-sqlite3. App so de frontend (HTML/JS puro) sem backend -> use localStorage/IndexedDB.\n"
    "   - SUPABASE (Postgres na nuvem, free) quando o usuario PEDIR nuvem/online/multiusuario, ou quando "
    "houver as variaveis SUPABASE_URL e SUPABASE_ANON_KEY no ambiente: use a lib oficial (supabase-js no "
    "front/Node, supabase-py no Python) lendo a URL e a anon key dessas variaveis (NUNCA escreva a chave no "
    "codigo). Crie as tabelas via SQL e faca CRUD real (select/insert/update/delete).\n"
    "   - Sempre crie o ESQUEMA (tabelas/migrations) e um SEED de exemplo, e garanta que criar/editar/excluir "
    "PERSISTE de verdade (sobrevive a recarregar a pagina/reiniciar). Diga ao usuario qual banco usou."
)

# Prompt LEVE para bate-papo (respostas rapidas, sem o peso das regras de codigo).
CHAT_PROMPT = (
    "Voce e a Kemy: uma parceira de IA brasileira, esperta, calorosa e com personalidade. "
    "Fala PORTUGUES do dia a dia, natural e com carisma — como uma amiga inteligente que manja "
    "de tecnologia, jogos e cria coisas com voce. Tem opiniao, bom humor leve e e direta: responde "
    "curto, sem enrolar, sem encher de pergunta. Trata o usuario pelo que importa, lembra do contexto "
    "e e proativa quando ajuda. NAO use emojis nem markdown pesado (a interface e limpa e sobria). "
    "Evita ser robotica ou formal demais; nada de 'Como posso ajudar?' generico. Se for pedido de "
    "criar/editar site ou codigo, abrir programa, jogar, controlar o PC etc., voce faz de boa. "
    "Seja confiante e gente boa, mas honesta: se algo nao da, fala na lata e sugere um caminho."
)

# Camada de DESIGN dedicada (estilo Claude artifacts). Anexada quando o pedido e claramente
# de UI/visual, pra elevar de "site simples" para design nivel produto/premiado.
DESIGN_PROMPT = (
    "\n\n=== MODO DESIGN (capriche como um DESIGNER DE PRODUTO SENIOR / site premiado Awwwards) ===\n"
    "Trate isto como um trabalho de design de verdade, com SISTEMA DE DESIGN, nao um HTML qualquer.\n"
    "1) DESIGN TOKENS em :root — paleta completa (bg, surface, surface-2, primary, primary-600, "
    "accent, text, text-muted, border, success/warn/danger), com tema coerente ao negocio e bom contraste "
    "(WCAG AA). Ofereca modo claro E escuro quando fizer sentido (prefers-color-scheme).\n"
    "2) ESCALA tipografica e de espacamento consistentes (base 4/8px: --space-1..8; --fs-1..7 com clamp() "
    "fluido). Use 1-2 Google Fonts modernas (ex.: Sora/Space Grotesk pra titulo + Inter pra corpo).\n"
    "3) GRID e ritmo: container max-width ~1200px, secoes com respiro generoso (80-120px), alinhamento "
    "impecavel, nada amontoado nem solto demais. Hierarquia visual CLARA (o olho sabe pra onde ir).\n"
    "4) COMPONENTES caprichados e CONSISTENTES: botoes (primario/secundario/ghost) com estados "
    "hover/active/focus-visible; cards com elevacao sutil e borda 1px translucida; inputs bonitos; "
    "navbar translucida (backdrop-filter blur); badges, tabs; bordas 12-20px; sombras suaves em camadas.\n"
    "5) DETALHES PRO: micro-interacoes (transition 150-250ms ease), hover que eleva/realca, gradientes ricos "
    "e/ou malha de cor (mesh), glassmorphism com parcimonia, ruido/grain sutil, scroll-reveal (IntersectionObserver), "
    "estados de foco acessiveis. Nada de efeito brega/exagerado.\n"
    "6) CONTEUDO real e convincente (copy boa em PT-BR, numeros plausiveis, depoimentos), nunca 'lorem ipsum' "
    "nem 'Item 1/2/3'. Imagens contextuais via Flux (pollinations) quando ajudar.\n"
    "7) RESPONSIVO de verdade (mobile-first, breakpoints 640/768/1024), e impecavel no mobile.\n"
    "8) O resultado tem que dar a sensacao de 'uau, parece um produto real de empresa top'. Capriche no "
    "acabamento como se fosse pro portfolio.\n"
)

# Palavras que indicam pedido de criar/editar codigo ou executar algo (usa o prompt completo).
BUILD_HINTS = (
    "site", "página", "pagina", "landing", "app", "aplicativo", "programa", "código",
    "codigo", "html", "css", "javascript", "script", "jogo", "game", "dashboard", "crud",
    "api", "crie", "cria", "criar", "gere", "gera", "gerar", "faça", "faca", "fazer",
    "monte", "montar", "desenvolva", "construa", "edite", "editar", "altere", "alterar",
    "conserte", "corrija", "abra", "abrir", "instale", "instalar", "rode", "rodar", "execute",
    "thumb", "thumbnail", "capa", "miniatura", "logo", "imagem", "foto", "desenho", "arte",
    "melhore", "melhora", "ajuste", "ajusta", "muda", "mude", "mudar", "deixa", "deixe",
    "refaça", "refaca", "refatore", "estilize", "estiliza", "design", "função", "funcao",
    "componente", "tela", "botão", "botao", "formulário", "formulario", "backend", "frontend",
    "doc", "documento", "pdf", "slide", "slides", "apresentação", "apresentacao", "planilha",
    "relatório", "relatorio", "currículo", "curriculo", "carta", "contrato", "proposta",
    "poster", "pôster", "banner", "flyer", "panfleto", "anúncio", "anuncio", "post",
    "story", "stories", "cartaz", "feed", "criativo", "identidade visual",
)


def is_build_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in BUILD_HINTS)


FILE_RE = re.compile(r"<<<FILE:\s*(.+?)>>>\s*\n(.*?)<<<END>>>", re.DOTALL)
EDIT_RE = re.compile(r"<<<EDIT:\s*(.+?)>>>\s*\n(.*?)<<<ENDEDIT>>>", re.DOTALL)
SR_RE = re.compile(r"<<<SEARCH>>>\s*\n(.*?)\n<<<REPLACE>>>\s*\n(.*?)(?=\n?<<<SEARCH>>>|\Z)", re.DOTALL)


def parse_edits(text: str) -> list:
    """Le blocos de edicao cirurgica:
    <<<EDIT: caminho>>>
    <<<SEARCH>>>
    codigo atual exato
    <<<REPLACE>>>
    codigo novo
    <<<ENDEDIT>>>
    (pode ter varios SEARCH/REPLACE no mesmo EDIT)."""
    out = []
    if not text:
        return out
    for m in EDIT_RE.finditer(text):
        path = m.group(1).strip()
        body = m.group(2)
        edits = []
        for sr in SR_RE.finditer(body):
            search = sr.group(1).rstrip("\n")
            replace = sr.group(2).rstrip("\n")
            if search:
                edits.append((search, replace))
        if path and edits:
            out.append({"path": path, "edits": edits})
    return out
_FNAME = r"[\w./\-]+\.(?:html?|css|js|jsx|tsx?|json|py|md|txt)"


def parse_loose_files(text: str) -> list[dict]:
    """Fallback tolerante: pega 'NOME.ext' (em **bold**, ### ou cru) seguido do conteudo,
    terminando em <<<END>>>, no proximo cabecalho de arquivo, ou no fim."""
    files: list[dict] = []
    # normaliza terminadores soltos
    headers = list(re.finditer(rf"(?:\*\*|`|#{{1,4}}\s*|//\s*)?({_FNAME})(?:\*\*|`)?\s*:?\s*\n", text))
    for i, h in enumerate(headers):
        name = h.group(1).strip()
        start = h.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        body = re.split(r"<<<END>>>", body)[0]
        body = re.sub(r"^\s*```[a-zA-Z0-9]*\s*\n", "", body)
        body = re.sub(r"\n?```\s*$", "", body.rstrip())
        body = body.strip("\n")
        if len(body) > 15:  # ignora cabecalho sem conteudo
            files.append({"path": name, "content": body + "\n"})
    return files


def _clean_chat(text: str) -> str:
    """Remove marcadores de arquivo/edicao que por acaso vazaram para o texto de conversa."""
    text = EDIT_RE.sub("", text)
    text = re.sub(r"<<<FILE:.*?>>>", "", text)
    text = text.replace("<<<END>>>", "").replace("<<<ENDEDIT>>>", "")
    text = re.sub(r"<<<(?:SEARCH|REPLACE)>>>", "", text)
    text = re.sub(rf"^\s*(?:\*\*|`|#{{1,4}}\s*)?{_FNAME}(?:\*\*|`)?\s*:?\s*$", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_llm_files(text: str) -> tuple[list[dict], str]:
    """Separa os arquivos (formato <<<FILE>>>) do texto de conversa."""
    text_no_edits = EDIT_RE.sub("", text)  # nao confundir edits com arquivos novos
    files = [{"path": m.group(1).strip(), "content": m.group(2).strip("\n") + "\n"}
             for m in FILE_RE.finditer(text_no_edits)]
    chat = FILE_RE.sub("", text_no_edits).strip()
    if not files:  # fallback 1: blocos markdown ```lang
        cf = extract_code_files(text)
        if cf:
            files = cf
            chat = re.sub(r"```[a-zA-Z0-9_+\-]*[ \t]*\n.*?```", "", text, flags=re.DOTALL).strip()
    if not files:  # fallback 2: cabecalhos soltos tipo **index.html**
        lf = parse_loose_files(text)
        if lf:
            files = lf
    # limpa blocos de comando crus e marcadores vazados
    chat = re.sub(r"```(?:kemy-run|run)\s*\n.*?```", "", chat, flags=re.DOTALL | re.IGNORECASE)
    chat = _clean_chat(chat)
    return files, chat


def extract_excel(path: Path, max_rows: int = 200) -> str:
    """Extrai os dados de uma planilha .xlsx como texto (tabela)."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets[:4]:
            out.append(f"# Planilha: {ws.title}")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= max_rows:
                    out.append("… (mais linhas)")
                    break
                out.append(" | ".join("" if c is None else str(c) for c in row))
        return "\n".join(out)[:15000]
    except Exception as exc:
        return f"(nao consegui ler a planilha: {exc})"


def fix_py_leading_zeros(src: str) -> tuple[str, bool]:
    """Conserta o erro classico de IA em Python 3: inteiros com ZERO A ESQUERDA
    (ex.: date(2023, 12, 01) -> 1). Compila, pega a linha do erro e remove o zero,
    repetindo ate compilar. Deterministico e seguro (so mexe na linha do erro)."""
    changed = False
    for _ in range(300):
        try:
            compile(src, "<kemy>", "exec")
            break
        except SyntaxError as e:
            if not (e.msg and "leading zeros" in e.msg and e.lineno):
                break
            lines = src.split("\n")
            idx = e.lineno - 1
            if idx < 0 or idx >= len(lines):
                break
            # remove zeros a esquerda de inteiros (nao mexe em 0.x, 0x.., nem casas decimais)
            new_line = re.sub(r"(?<![\w.])0+(\d)", r"\1", lines[idx])
            if new_line == lines[idx]:
                break
            lines[idx] = new_line
            src = "\n".join(lines)
            changed = True
        except Exception:
            break
    return src, changed


def audit_dead_controls(base: Path) -> list[str]:
    """Acha botoes/links 'mortos' em templates HTML (deterministico, alta precisao):
    href vazio/#, e referencias {% url 'nome' %} para rotas que NAO existem nas urls.py.
    Retorna uma lista curta de problemas pra mandar a IA ligar tudo."""
    issues: list[str] = []
    try:
        htmls = [p for p in base.rglob("*.html")][:80]
    except Exception:
        return issues
    # nomes de rota definidos (Django: name="..."/path('', ..., name='x'))
    defined: set = set()
    try:
        for up in base.rglob("urls.py"):
            txt = up.read_text(encoding="utf-8", errors="ignore")
            defined |= set(re.findall(r"name\s*=\s*['\"]([\w:.\-]+)['\"]", txt))
    except Exception:
        pass
    for h in htmls:
        try:
            t = h.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = str(h.relative_to(base))
        dead = len(re.findall(r"href\s*=\s*['\"](?:#||javascript:void\(0\))['\"]", t))
        # botoes/links com texto de acao mas sem destino real
        action_dead = len(re.findall(r"(?i)>\s*(criar|nova|novo|adicionar|editar|excluir|deletar|salvar)\b[^<]*<", t)) \
            if (dead or "href=\"#\"" in t) else 0
        if dead:
            issues.append(f"{rel}: {dead} link(s) com href vazio/# (botao sem acao).")
        for name in set(re.findall(r"{%\s*url\s+['\"]([\w:.\-]+)['\"]", t)):
            if defined and name not in defined:
                issues.append(f"{rel}: {{% url '{name}' %}} aponta para rota inexistente.")
    return issues[:40]


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


def relevant_project_files(base: Path, text: str, max_total: int = 22000) -> str:
    """RAG de codebase: em projetos grandes, inclui so os arquivos MAIS RELEVANTES ao pedido
    (por nome/caminho + arquivos de entrada), em vez de despejar tudo."""
    exts = (".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".json", ".py", ".md", ".txt",
            ".java", ".c", ".cpp", ".cs", ".php", ".rb", ".go", ".rs", ".sql", ".vue", ".svelte")
    if not base.exists():
        return ""
    files = [p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("_")]
    if not files:
        return ""
    total_size = sum((p.stat().st_size for p in files), 0)
    if total_size <= max_total:                       # projeto pequeno -> tudo
        return read_project_files(base, max_total)
    words = set(re.findall(r"[\wáéíóúâêôãõç]{3,}", (text or "").lower()))
    entry = {"index.html", "main.py", "app.py", "app.js", "script.js", "styles.css", "package.json"}

    def score(p: Path) -> int:
        s = 5 if p.name.lower() in entry else 0
        low = str(p.relative_to(base)).lower()
        s += sum(1 for w in words if w in low)
        return s
    files.sort(key=score, reverse=True)
    parts, total, included = [], 0, []
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        block = f"<<<FILE: {p.relative_to(base)}>>>\n{txt}\n<<<END>>>\n"
        if total + len(block) > max_total:
            continue
        parts.append(block)
        total += len(block)
        included.append(str(p.relative_to(base)))
    todos = [str(p.relative_to(base)) for p in files]
    faltando = [f for f in todos if f not in included]
    listagem = ("\n[Projeto grande: incluí os arquivos mais relevantes. Outros arquivos existentes: "
                + ", ".join(faltando[:40]) + ". Peça por um arquivo específico se precisar editá-lo.]\n") if faltando else ""
    return "".join(parts) + listagem


class LLMClient:
    def __init__(self, env: dict[str, str]) -> None:
        # Uma chave NVIDIA comeca SEMPRE com 'nvapi-'. Se ela for colada no campo errado
        # (ex.: MISTRAL_API_KEY=nvapi-...), nao serve pra aquele provedor — ignoramos ali e
        # recolhemos pra NVIDIA. Assim a configuracao fica a prova de erro.
        def _nonnv(*vals):
            for v in vals:
                if v and not str(v).strip().startswith("nvapi-"):
                    return v
            return None

        self.gemini = _nonnv(env.get("GEMINI_API_KEY"))
        self.groq = _nonnv(env.get("GROQ_API_KEY"))
        self.cerebras = _nonnv(env.get("CEREBRAS_API_KEY"))
        self.openai = _nonnv(env.get("OPENAI_API_KEY"), env.get("CHATGPT_API_KEY"))
        self.openrouter = _nonnv(env.get("OPENROUTER_API_KEY"))
        self.mistral = _nonnv(env.get("MISTRAL_API_KEY"))
        self.github = _nonnv(env.get("GITHUB_MODELS_TOKEN"), env.get("GITHUB_TOKEN"), env.get("GH_TOKEN"))
        self.sambanova = _nonnv(env.get("SAMBANOVA_API_KEY"), env.get("SAMBA_API_KEY"))
        self.anthropic = _nonnv(env.get("ANTHROPIC_API_KEY"), env.get("CLAUDE_API_KEY"))
        # NVIDIA: recolhe TODA chave 'nvapi-' de QUALQUER variavel do ambiente (mesmo se posta
        # no campo errado) + NVIDIA_API_KEY/NIM_API_KEY/NVIDIA_API_KEY2..9. Roda entre elas.
        pool: list[str] = []
        for v in env.values():
            if isinstance(v, str) and "nvapi-" in v:
                pool += [t for t in re.split(r"[\s,;]+", v) if t.startswith("nvapi-")]
        seen: set = set()
        self.nvidia_keys = [k for k in pool if not (k in seen or seen.add(k))]
        self.nvidia = self.nvidia_keys[0] if self.nvidia_keys else None
        self.available = bool(self.gemini or self.groq or self.cerebras or self.openai
                              or self.openrouter or self.nvidia or self.mistral
                              or self.github or self.sambanova or self.anthropic)
        # Nano Banana (Gemini Image) usa a chave do Gemini — vira o gerador de imagem padrao.
        if self.gemini:
            global GEMINI_IMAGE_KEY
            GEMINI_IMAGE_KEY = self.gemini

        def _list(key: str, default: list[str]) -> list[str]:
            raw = (env.get(key) or "").strip()
            picked = [m.strip() for m in raw.split(",") if m.strip()] if raw else []
            # mantem o conhecido-bom no fim como rede de seguranca
            for d in default:
                if d not in picked:
                    picked.append(d)
            return picked

        # Listas preferenciais: modelos de CODIGO/recentes primeiro, fallback estavel no fim.
        # Sobrescreva por env (CEREBRAS_MODEL, GROQ_MODEL, OPENROUTER_MODEL, GEMINI_PRIMARY_MODEL),
        # virgula-separado, na ordem de preferencia.
        self.cerebras_models = _list("CEREBRAS_MODEL", ["qwen-3-coder-480b", "gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507"])
        # Kimi K2 lidera o Groq pra CODIGO (excelente modelo de codigo, gratis e rapido).
        self.groq_models = _list("GROQ_MODEL", ["moonshotai/kimi-k2-instruct", "openai/gpt-oss-120b", "qwen/qwen3-32b"])
        self.openrouter_models = _list("OPENROUTER_MODEL", ["qwen/qwen3-coder:free", "qwen/qwen-2.5-coder-32b-instruct:free"])
        # NVIDIA NIM (build.nvidia.com) — OpenAI-compatible, tier gratis. MODELOS DE FRONTEIRA
        # (nivel Claude/GPT) abertos: DeepSeek-V4-Pro 1.6T, GLM-5.1 754B, Mistral-Large-3 675B.
        # IDs alternativos ficam na lista: o que nao existir na conta falha e cai pro proximo.
        self.nvidia_models = _list("NVIDIA_MODEL", [
            "deepseek-ai/deepseek-v4-pro", "mistralai/mistral-large-3-675b-instruct-2512",
            "zai-org/glm-5.1", "z-ai/glm-5.1", "qwen/qwen2.5-coder-32b-instruct"])
        self.nvidia_fast = _list("NVIDIA_FAST", [
            "zai-org/glm-5.1", "z-ai/glm-5.1", "qwen/qwen2.5-coder-32b-instruct",
            "mistralai/mistral-large-3-675b-instruct-2512"])
        # Mistral (api.mistral.ai) — OpenAI-compatible, free tier ~1B tokens/mes. Codestral e otimo pra codigo.
        self.mistral_models = _list("MISTRAL_MODEL", ["codestral-latest", "mistral-large-latest", "mistral-small-latest"])
        self.mistral_fast = _list("MISTRAL_FAST", ["mistral-small-latest", "open-mistral-nemo"])
        # GitHub Models (models.github.ai) — GPT-5/GPT-4o/o3/DeepSeek de graca via PAT do GitHub.
        self.github_models = _list("GITHUB_MODEL", ["openai/gpt-5", "openai/gpt-4o", "openai/o3-mini", "deepseek/DeepSeek-V3-0324"])
        self.github_fast = _list("GITHUB_FAST", ["openai/gpt-5-mini", "openai/gpt-4o-mini", "openai/gpt-4o"])
        # SambaNova (api.sambanova.ai) — DeepSeek/Qwen rapidos, tier gratis.
        self.sambanova_models = _list("SAMBANOVA_MODEL", ["DeepSeek-V3-0324", "Qwen2.5-Coder-32B-Instruct", "DeepSeek-R1"])
        self.sambanova_fast = _list("SAMBANOVA_FAST", ["Qwen2.5-Coder-32B-Instruct", "DeepSeek-V3-0324"])
        # Lidera com o MAIS ATUAL e gratis: 'gemini-flash-latest' (alias que aponta pro mais novo,
        # = Gemini 3 Flash quando disponivel, sem dar 404) e 'gemini-3-flash'; cai pra 2.5/2.0 como
        # rede de seguranca. O Gemini 3 PRO via API e PAGO -> opt-in: GEMINI_PRIMARY_MODEL=gemini-3-pro
        self.gemini_models = _list("GEMINI_PRIMARY_MODEL",
                                   ["gemini-flash-latest", "gemini-3-flash", "gemini-2.5-flash", "gemini-2.0-flash"])
        self.openai_models = _list("OPENAI_MODEL", ["gpt-4o-mini"])
        # Claude (Anthropic API, PAGO) — melhor pra codigo. Use chave ANTHROPIC_API_KEY.
        self.claude_models = _list("CLAUDE_MODEL", ["claude-sonnet-4-6", "claude-3-5-sonnet-latest"])
        self.claude_fast = _list("CLAUDE_FAST", ["claude-haiku-4-5-20251001", "claude-3-5-haiku-latest"])
        # Modelos para bate-papo/voz: inteligentes E rapidos (GPT-OSS 120B segura bem o
        # contexto e responde em ~1-2s); Llama so como ultimo fallback.
        self.cerebras_fast = _list("CEREBRAS_FAST", ["gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507"])
        self.groq_fast = _list("GROQ_FAST", ["openai/gpt-oss-120b", "qwen/qwen3-32b", "moonshotai/kimi-k2-instruct"])
        self.gemini_model = self.gemini_models[0]
        self._working: dict[str, str] = {}  # provedor -> modelo que funcionou

    def primary_label(self) -> str:
        """Provedor + modelo que sera tentado primeiro (mesma ordem do chat())."""
        if self.cerebras:
            return f"Cerebras · {self.cerebras_models[0]}"
        if self.groq:
            return f"Groq · {self.groq_models[0]}"
        if self.gemini:
            return f"Gemini · {self.gemini_models[0]}"
        if self.nvidia:
            return f"NVIDIA · {self.nvidia_models[0]}"
        if self.github:
            return f"GitHub · {self.github_models[0]}"
        if self.sambanova:
            return f"SambaNova · {self.sambanova_models[0]}"
        if self.mistral:
            return f"Mistral · {self.mistral_models[0]}"
        if self.openai:
            return f"OpenAI · {self.openai_models[0]}"
        if self.openrouter:
            return f"OpenRouter · {self.openrouter_models[0]}"
        return "IA"

    def providers(self) -> list[str]:
        out = []
        for name, key in (("cerebras", self.cerebras), ("groq", self.groq), ("gemini", self.gemini),
                          ("nvidia", self.nvidia), ("github", self.github), ("sambanova", self.sambanova),
                          ("mistral", self.mistral), ("openrouter", self.openrouter),
                          ("openai", self.openai), ("anthropic", self.anthropic)):
            if key:
                out.append(name)
        return out

    def chat(self, system: str, messages: list[dict], max_tokens: int = 16000, fast: bool = False,
             prefer: str = "") -> str:
        errors: list[str] = []
        # fast=True (bate-papo/voz) usa modelos menores e rapidos; senao usa os de codigo.
        cb_models = self.cerebras_fast if fast else self.cerebras_models
        gq_models = self.groq_fast if fast else self.groq_models
        nv_models = self.nvidia_fast if fast else self.nvidia_models
        ms_models = self.mistral_fast if fast else self.mistral_models
        gh_models = self.github_fast if fast else self.github_models
        sn_models = self.sambanova_fast if fast else self.sambanova_models
        attempts: list[tuple[str, str, object]] = []

        def add(prov: str, models: list[str], maker) -> None:
            wk = self._working.get(("fast:" if fast else "") + prov)
            chosen = [wk] if wk in models else models
            for m in chosen:
                attempts.append((prov, m, maker(m)))

        def add_gemini() -> None:
            if self.gemini:
                add("gemini", self.gemini_models, lambda m: (lambda: self._gemini(system, messages, m, max_tokens)))

        def add_nvidia() -> None:
            if self.nvidia_keys:
                add("nvidia", nv_models, lambda m: (lambda: self._nvidia_compat(m, system, messages, max_tokens)))

        # Conversa (fast): Gemini Flash primeiro (rapido, segura contexto); cai pro Cerebras.
        # Codigo/build (nao-fast): NVIDIA primeiro (modelos de fronteira = melhor qualidade);
        # quando os creditos NVIDIA esgotarem, cai pro Cerebras/Groq (gratis e sem expirar).
        if fast:
            add_gemini()
        else:
            add_nvidia()
        if self.cerebras:
            add("cerebras", cb_models, lambda m: (lambda: self._openai_compat(
                "https://api.cerebras.ai/v1/chat/completions", self.cerebras, m, system, messages, max_tokens)))
        if self.groq:
            add("groq", gq_models, lambda m: (lambda: self._openai_compat(
                "https://api.groq.com/openai/v1/chat/completions", self.groq, m, system, messages, max_tokens)))
        if fast:
            add_nvidia()
        if self.sambanova:
            add("sambanova", sn_models, lambda m: (lambda: self._openai_compat(
                "https://api.sambanova.ai/v1/chat/completions", self.sambanova, m, system, messages, max_tokens)))
        if self.github:
            add("github", gh_models, lambda m: (lambda: self._openai_compat(
                "https://models.github.ai/inference/chat/completions", self.github, m, system, messages, max_tokens)))
        if self.mistral:
            add("mistral", ms_models, lambda m: (lambda: self._openai_compat(
                "https://api.mistral.ai/v1/chat/completions", self.mistral, m, system, messages, max_tokens)))
        if not fast:
            add_gemini()
        if self.openai:
            add("openai", self.openai_models, lambda m: (lambda: self._openai_compat(
                "https://api.openai.com/v1/chat/completions", self.openai, m, system, messages, max_tokens)))
        if self.openrouter:
            add("openrouter", self.openrouter_models, lambda m: (lambda: self._openai_compat(
                "https://openrouter.ai/api/v1/chat/completions", self.openrouter, m, system, messages, max_tokens)))
        if prefer:  # revisao cruzada: tenta um provedor diferente primeiro
            attempts.sort(key=lambda a: 0 if a[0] == prefer else 1)
        for prov, model, fn in attempts:
            try:
                res = fn()
                self._working[("fast:" if fast else "") + prov] = model
                return res
            except Exception as exc:
                errors.append(f"{prov}/{model}: {exc}")
        # Diagnostico claro de QUAIS provedores tem chave (NVIDIA some quando nao ha chave nvapi-).
        have = [n for n, k in (("nvidia", self.nvidia), ("cerebras", self.cerebras), ("groq", self.groq),
                               ("gemini", self.gemini), ("mistral", self.mistral), ("github", self.github),
                               ("sambanova", self.sambanova), ("openai", self.openai),
                               ("openrouter", self.openrouter)) if k]
        diag = (f"\n\nProvedores COM chave: {', '.join(have) or 'nenhum'}."
                + ("" if self.nvidia else " ⚠️ NVIDIA SEM chave: nenhuma chave 'nvapi-' foi "
                   "encontrada no .env/secrets — confira o NVIDIA_API_KEY."))
        raise RuntimeError(
            ("Todos os provedores falharam (" + "; ".join(errors) + ")." + diag)
            if errors else "Sem provedor de IA configurado." + diag)

    def _post(self, url: str, headers: dict, payload: dict, timeout: float = 60) -> dict:
        data = json.dumps(payload).encode("utf-8")
        # User-Agent de navegador: evita o bloqueio 403/1010 do Cloudflare (Cerebras/Groq).
        headers = {"User-Agent": BROWSER_UA, "Accept": "application/json", **headers}
        last_err: Exception | None = None
        # Retry com backoff em 429/503 (limites momentaneos sao comuns no free tier).
        for attempt in range(4):
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (429, 503) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                try:
                    body = exc.read().decode("utf-8", "ignore")[:180]
                except Exception:
                    body = ""
                raise RuntimeError(f"HTTP {exc.code} {body}".strip()) from exc
            except Exception as exc:
                last_err = exc
                if attempt < 3:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise last_err or RuntimeError("falha desconhecida")

    def _nvidia_compat(self, model: str, system: str, messages: list[dict], max_tokens: int = 16000) -> str:
        """Chama a NVIDIA NIM rodando entre TODAS as chaves: se uma estiver sem credito
        (401/402/403/429), tenta a proxima. Lembra qual funcionou pra usar primeiro."""
        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        wk = self._working.get("nvidia_key")
        order = ([wk] if wk in self.nvidia_keys else []) + [k for k in self.nvidia_keys if k != wk]
        last_err: Exception | None = None
        for key in order:
            try:
                res = self._openai_compat(url, key, model, system, messages, max_tokens)
                self._working["nvidia_key"] = key
                return res
            except Exception as exc:
                last_err = exc
                continue
        raise last_err or RuntimeError("sem chave NVIDIA")

    def _gemini(self, system: str, messages: list[dict], model: str | None = None, max_tokens: int = 16000) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model or self.gemini_model}:generateContent?key={self.gemini}")
        contents = [{"role": "model" if m["role"] == "assistant" else "user",
                     "parts": [{"text": m["content"]}]} for m in messages]
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.6, "maxOutputTokens": min(16384, max_tokens)},
        }
        data = self._post(url, {"Content-Type": "application/json"}, payload)
        return data["candidates"][0]["content"]["parts"][0]["text"]

    def vision(self, prompt: str, image_b64: str, mime: str) -> str:
        """Analisa uma imagem (multimodal). Usa Gemini (free tier suporta visao)."""
        if not self.gemini:
            raise RuntimeError("Para enviar imagens, configure a chave do Gemini (GEMINI_API_KEY).")
        for model in self.gemini_models:
            url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
                   f"{model}:generateContent?key={self.gemini}")
            payload = {"contents": [{"role": "user", "parts": [
                {"text": prompt or "Descreva esta imagem em portugues e como posso usa-la."},
                {"inline_data": {"mime_type": mime, "data": image_b64}},
            ]}], "generationConfig": {"temperature": 0.5, "maxOutputTokens": 4096}}
            try:
                data = self._post(url, {"Content-Type": "application/json"}, payload)
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                continue
        raise RuntimeError("Nao consegui analisar a imagem com o Gemini.")

    def _openai_compat(self, url: str, key: str, model: str, system: str, messages: list[dict], max_tokens: int = 16000) -> str:
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        payload = {"model": model, "messages": msgs, "temperature": 0.6, "max_tokens": max_tokens}
        data = self._post(url, {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, payload)
        return data["choices"][0]["message"]["content"]

    # ---- Streaming (resposta em tempo real) ----
    def chat_stream(self, system: str, messages: list[dict], on_chunk, max_tokens: int = 700, fast: bool = True) -> str:
        cb = self.cerebras_fast if fast else self.cerebras_models
        gq = self.groq_fast if fast else self.groq_models
        nv = self.nvidia_fast if fast else self.nvidia_models
        ms = self.mistral_fast if fast else self.mistral_models
        gh = self.github_fast if fast else self.github_models
        sn = self.sambanova_fast if fast else self.sambanova_models
        order = []
        if fast and self.gemini:
            order.append(("gemini", self.gemini_models[0]))
        if self.cerebras:
            order.append(("cerebras", cb[0]))
        if self.groq:
            order.append(("groq", gq[0]))
        if self.nvidia:
            order.append(("nvidia", nv[0]))
        if self.sambanova:
            order.append(("sambanova", sn[0]))
        if self.github:
            order.append(("github", gh[0]))
        if self.mistral:
            order.append(("mistral", ms[0]))
        if not fast and self.gemini:
            order.append(("gemini", self.gemini_models[0]))
        if self.openrouter:
            order.append(("openrouter", self.openrouter_models[0]))
        if self.openai:
            order.append(("openai", self.openai_models[0]))
        urls = {"cerebras": "https://api.cerebras.ai/v1/chat/completions",
                "groq": "https://api.groq.com/openai/v1/chat/completions",
                "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
                "sambanova": "https://api.sambanova.ai/v1/chat/completions",
                "github": "https://models.github.ai/inference/chat/completions",
                "mistral": "https://api.mistral.ai/v1/chat/completions",
                "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                "openai": "https://api.openai.com/v1/chat/completions"}
        keys = {"cerebras": self.cerebras, "groq": self.groq, "nvidia": self.nvidia,
                "sambanova": self.sambanova, "github": self.github,
                "mistral": self.mistral, "openrouter": self.openrouter, "openai": self.openai}
        errs = []
        for prov, model in order:
            try:
                if prov == "gemini":
                    return self._gemini_stream(system, messages, model, on_chunk, max_tokens)
                return self._openai_stream(urls[prov], keys[prov], model, system, messages, on_chunk, max_tokens)
            except Exception as exc:
                errs.append(f"{prov}: {exc}")
        raise RuntimeError("stream falhou (" + "; ".join(errs) + ")")

    def _openai_stream(self, url, key, model, system, messages, on_chunk, max_tokens) -> str:
        msgs = [{"role": "system", "content": system}] + [{"role": m["role"], "content": m["content"]} for m in messages]
        payload = {"model": model, "messages": msgs, "temperature": 0.6, "max_tokens": max_tokens, "stream": True}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}",
                                              "User-Agent": BROWSER_UA, "Accept": "text/event-stream"},
                                     method="POST")
        full = ""
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                try:
                    delta = json.loads(d)["choices"][0]["delta"].get("content", "")
                except Exception:
                    continue
                if delta:
                    full += delta
                    on_chunk(delta)
        if not full:
            raise RuntimeError("stream vazio")
        return full

    def _gemini_stream(self, system, messages, model, on_chunk, max_tokens) -> str:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:streamGenerateContent?alt=sse&key={self.gemini}")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages]
        payload = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents,
                   "generationConfig": {"temperature": 0.6, "maxOutputTokens": min(8192, max_tokens)}}
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
        full = ""
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if not d:
                    continue
                try:
                    t = json.loads(d)["candidates"][0]["content"]["parts"][0]["text"]
                except Exception:
                    continue
                if t:
                    full += t
                    on_chunk(t)
        if not full:
            raise RuntimeError("gemini stream vazio")
        return full


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

        # cabelo gotico longo (atras do rosto) - sem calvicie
        c.coords(self.back_hair,
                 cx - rx * 1.45, cy - ry * 0.7, cx - rx * 1.5, cy + ry * 1.75,
                 cx - rx * 0.7, cy + ry * 1.15, cx, cy + ry * 1.3,
                 cx + rx * 0.7, cy + ry * 1.15, cx + rx * 1.5, cy + ry * 1.75,
                 cx + rx * 1.45, cy - ry * 0.7, cx + rx * 0.8, cy - ry * 1.3,
                 cx, cy - ry * 1.52, cx - rx * 0.8, cy - ry * 1.3)
        c.itemconfig(self.back_hair, fill=COLORS["hair"])

        # rosto palido (mais estreito = elegante)
        self._ov(c, self.head, cx, cy, rx * 0.9, ry)
        c.itemconfig(self.head, fill=COLORS["skin"])

        # pods tech laterais com nucleo neon
        ear_y = cy + ry * 0.05
        ex = rx * 0.98
        cup_pulse = (3 * (0.5 + 0.5 * math.sin(t * 4))) if self.state == "listening" else 0
        self._ov(c, self.ear_l, cx - ex, ear_y, S * 0.05, S * 0.085)
        self._ov(c, self.ear_r, cx + ex, ear_y, S * 0.05, S * 0.085)
        c.itemconfig(self.ear_l, fill=COLORS["hair_dark"])
        c.itemconfig(self.ear_r, fill=COLORS["hair_dark"])
        self._ov(c, self.cup_l, cx - ex, ear_y, S * 0.016 + cup_pulse, S * 0.05 + cup_pulse)
        self._ov(c, self.cup_r, cx + ex, ear_y, S * 0.016 + cup_pulse, S * 0.05 + cup_pulse)
        c.itemconfig(self.cup_l, fill=accent)
        c.itemconfig(self.cup_r, fill=accent)

        # arco neon (gola futurista) sob o queixo
        c.coords(self.band, cx - rx * 1.15, cy + ry * 0.25, cx + rx * 1.15, cy + ry * 1.55)
        c.itemconfig(self.band, outline=accent, start=200, extent=140, width=3)

        # franja/cabelo cobrindo a coroa inteira (widow's peak baixo) - sem careca
        c.coords(self.bangs,
                 cx - rx * 1.04, cy - ry * 0.15,
                 cx - rx * 1.12, cy - ry * 1.05,
                 cx - rx * 0.4, cy - ry * 1.3,
                 cx, cy - ry * 1.36,
                 cx + rx * 0.4, cy - ry * 1.3,
                 cx + rx * 1.12, cy - ry * 1.05,
                 cx + rx * 1.04, cy - ry * 0.15,
                 cx + rx * 0.5, cy - ry * 0.45,
                 cx, cy - ry * 0.02,
                 cx - rx * 0.5, cy - ry * 0.45)
        c.itemconfig(self.bangs, fill=COLORS["hair"])

        # gema futurista na testa (pulsa)
        gx, gy = cx, cy - ry * 0.4
        gs = S * 0.02 + S * 0.006 * (0.5 + 0.5 * math.sin(t * 3))
        c.coords(self.tuft, gx, gy - gs, gx + gs * 0.7, gy, gx, gy + gs, gx - gs * 0.7, gy)
        c.itemconfig(self.tuft, fill=accent)

        # olhos neon
        blink = self._blink_factor(t)
        eye_y = cy + ry * 0.08
        eye_dx = rx * 0.42
        ew, eh = S * 0.05, S * 0.05 * blink
        self._ov(c, self.eye_l, cx - eye_dx, eye_y, ew, max(eh, 1))
        self._ov(c, self.eye_r, cx + eye_dx, eye_y, ew, max(eh, 1))
        c.itemconfig(self.eye_l, fill=COLORS["eye_white"])
        c.itemconfig(self.eye_r, fill=COLORS["eye_white"])
        look_y = -S * 0.016 if self.state == "thinking" else 0
        ir = S * 0.032 * (1 if blink > 0.4 else 0.2)
        self._ov(c, self.iris_l, cx - eye_dx, eye_y + look_y, ir, ir)
        self._ov(c, self.iris_r, cx + eye_dx, eye_y + look_y, ir, ir)
        c.itemconfig(self.iris_l, fill=accent)
        c.itemconfig(self.iris_r, fill=accent)
        hr = ir * 0.45
        self._ov(c, self.hi_l, cx - eye_dx + ir * 0.35, eye_y + look_y - ir * 0.35, hr, hr)
        self._ov(c, self.hi_r, cx + eye_dx + ir * 0.35, eye_y + look_y - ir * 0.35, hr, hr)
        vis = "normal" if blink > 0.4 else "hidden"
        for it in (self.iris_l, self.iris_r, self.hi_l, self.hi_r):
            c.itemconfig(it, state=vis)

        # delineado angular (edgy)
        by = eye_y - eh - S * 0.022
        c.coords(self.brow_l, cx - eye_dx - ew * 1.1, by - 2, cx - eye_dx + ew * 0.8, by + 3)
        c.coords(self.brow_r, cx + eye_dx - ew * 0.8, by + 3, cx + eye_dx + ew * 1.1, by - 2)
        c.itemconfig(self.brow_l, fill=COLORS["hair_dark"])
        c.itemconfig(self.brow_r, fill=COLORS["hair_dark"])

        # blush sutil e frio
        self._ov(c, self.blush_l, cx - eye_dx, eye_y + S * 0.06, S * 0.022, S * 0.012)
        self._ov(c, self.blush_r, cx + eye_dx, eye_y + S * 0.06, S * 0.022, S * 0.012)
        c.itemconfig(self.blush_l, fill=COLORS["blush"])
        c.itemconfig(self.blush_r, fill=COLORS["blush"])
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
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U00002700-\U000027BF]+")


def clean_for_speech(text: str) -> str:
    """Limpa o texto para a fala soar humana: remove markdown, links, emojis e simbolos
    que a voz leria em voz alta (asterisco, hashtag, crase, etc.)."""
    if not text:
        return ""
    t = text
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)          # blocos de codigo
    t = re.sub(r"`[^`]*`", " ", t)                              # codigo inline
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)              # [texto](link) -> texto
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)                # urls
    t = re.sub(r"[*_#~>`|]+", " ", t)                            # simbolos de markdown
    t = re.sub(r"^\s*[-•·]\s*", "", t, flags=re.MULTILINE)       # bullets
    t = _EMOJI_RE.sub(" ", t)                                    # emojis
    t = t.replace("…", "...").replace("•", " ")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\s+([,.!?;:])", r"\1", t)                       # espaco antes de pontuacao
    t = re.sub(r"\n{2,}", ". ", t).replace("\n", " ")
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


class Speaker:
    """Voz da Kemy. Prefere Edge TTS (voz neural natural, gratis, precisa internet);
    cai para o pyttsx3/SAPI5 (offline, robotico) se o Edge falhar."""

    def __init__(self) -> None:
        self.on_start = None
        self.on_done = None
        self.log = None
        self._el_warned = False
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._engine = None
        self._speaking = False
        self._last_word = 0.0
        self.voice = os.environ.get("KEMY_VOICE", "pt-BR-FranciscaNeural")
        # ElevenLabs (voz de personagem premium) — se houver chave.
        self.el_key = os.environ.get("KEMY_ELEVENLABS_KEY") or os.environ.get("ELEVENLABS_API_KEY")
        self.el_voice = os.environ.get("KEMY_ELEVENLABS_VOICE", "EXAVITQu4vr4xnSDxMaL")
        self.el_model = os.environ.get("KEMY_ELEVENLABS_MODEL", "eleven_multilingual_v2")
        self._edge_ok = False
        if os.name == "nt" and os.environ.get("KEMY_VOICE_ENGINE", "edge") != "sapi":
            try:
                import edge_tts  # noqa: F401
                self._edge_ok = True
            except Exception:
                self._edge_ok = False
        self.available = bool(self.el_key) or self._edge_ok or VOICE_SUPPORT["tts"]
        if self.available:
            threading.Thread(target=self._loop, daemon=True).start()

    def _on_word(self, *_a, **_k) -> None:
        self._last_word = time.time()

    def mouth_level(self) -> float:
        if not self._speaking:
            return 0.0
        # Edge: nao temos eventos de palavra -> oscila para simular a fala.
        if self._edge_ok or self._engine is None:
            return 0.22 + 0.7 * abs(math.sin(time.time() * 11.0))
        dt = time.time() - self._last_word
        if dt < 0.13:
            return 1.0
        if dt < 0.26:
            return 0.55
        return 0.18

    def _loop(self) -> None:
        engine = None
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
            engine = None
            self._engine = None
            if not self._edge_ok:
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
            spoke = False
            if self.el_key:
                try:
                    self._speak_eleven(text)
                    spoke = True
                except Exception as exc:
                    spoke = False  # chave invalida/cota -> tenta edge
                    if self.log and not self._el_warned:
                        self._el_warned = True
                        self.log(f"⚠️ Voz ElevenLabs indisponivel ({exc}); usando a voz reserva.")
            if not spoke and self._edge_ok:
                try:
                    self._speak_edge(text)
                    spoke = True
                except Exception:
                    spoke = False  # sem internet/erro -> cai pro SAPI
            if not spoke and engine is not None:
                try:
                    engine.say(text)
                    engine.runAndWait()
                except Exception:
                    pass
            self._speaking = False
            if self.on_done and self._queue.empty():
                self.on_done()

    def _speak_edge(self, text: str) -> None:
        import asyncio
        import edge_tts
        path = os.path.join(tempfile.gettempdir(), f"kemy_tts_{uuid.uuid4().hex[:8]}.mp3")

        async def _gen() -> None:
            await edge_tts.Communicate(text, self.voice).save(path)

        asyncio.run(_gen())
        if not os.path.exists(path) or os.path.getsize(path) < 256:
            raise RuntimeError("edge-tts falhou")
        self._play_mp3(path)

    def _speak_eleven(self, text: str) -> None:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.el_voice}"
        body = json.dumps({"text": text, "model_id": self.el_model,
                           "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "xi-api-key": self.el_key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        with urllib.request.urlopen(req, timeout=15) as r:
            audio = r.read()
        if len(audio) < 256:
            raise RuntimeError("elevenlabs vazio")
        path = os.path.join(tempfile.gettempdir(), f"kemy_tts_{uuid.uuid4().hex[:8]}.mp3")
        with open(path, "wb") as f:
            f.write(audio)
        self._play_mp3(path)

    def _play_mp3(self, path: str) -> None:
        import ctypes
        alias = "kemyv" + uuid.uuid4().hex[:6]
        mci = ctypes.windll.winmm.mciSendStringW
        try:
            mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
            buf = ctypes.create_unicode_buffer(64)
            mci(f"status {alias} length", buf, 64, None)
            try:
                length = int(buf.value)
            except Exception:
                length = 0
            mci(f"play {alias}", None, 0, None)
            start = time.time()
            while self._speaking and (time.time() - start) * 1000 < length + 250:
                time.sleep(0.05)
        finally:
            try:
                mci(f"stop {alias}", None, 0, None)
                mci(f"close {alias}", None, 0, None)
            except Exception:
                pass
            try:
                os.remove(path)
            except Exception:
                pass

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not (self.available and text.strip()):
            return
        # Fala em frases (chunks) para o audio comecar rapido = sensacao de tempo real.
        parts = re.split(r"(?<=[.!?…\n])\s+", text.strip())
        chunk = ""
        for p in parts:
            p = p.strip()
            if not p:
                continue
            chunk = f"{chunk} {p}".strip() if chunk else p
            if len(chunk) >= 55:
                self._queue.put(chunk)
                chunk = ""
        if chunk:
            self._queue.put(chunk)

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
                # detecta o fim da fala mais rapido (menos espera apos voce parar)
                self._recognizer.pause_threshold = 0.55
                self._recognizer.non_speaking_duration = 0.3
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
                    self._recognizer.adjust_for_ambient_noise(source, duration=0.2)
                    audio = self._recognizer.listen(source, timeout=8, phrase_time_limit=10)
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
        keys = ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY",
                "NVIDIA_API_KEY", "MISTRAL_API_KEY", "GITHUB_MODELS_TOKEN", "GITHUB_TOKEN",
                "SAMBANOVA_API_KEY", "OPENAI_API_KEY")
        return any(self.env_file_vars.get(k) for k in keys)

    # ----- UI ----- #
    def _build_ui(self) -> None:
        self.root.title(f"Kemy - Assistente de Voz (classico {build_tag()})")
        self.root.geometry("1060x760")
        self.root.minsize(900, 640)
        self.root.configure(bg=COLORS["bg"])

        # Top bar
        top = tk.Frame(self.root, bg=COLORS["panel"], height=60)
        top.pack(fill="x", side="top")
        tk.Frame(self.root, bg=COLORS["line"], height=1).pack(fill="x", side="top")
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
                                  relief="flat", activebackground=COLORS["speaking"], padx=22, pady=10,
                                  cursor="hand2")
        self.talk_btn.pack(side="left")
        # toggles modernos (sem cara de checkbox do XP)
        self.continuous = False
        self.autonomous = False
        self.conv_toggle = tk.Button(controls, text="💬 Conversa", command=self._toggle_continuous,
                                     bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold"),
                                     relief="flat", padx=14, pady=8, cursor="hand2",
                                     activebackground=COLORS["panel_soft"])
        self.conv_toggle.pack(side="left", padx=(10, 6))
        self.auto_toggle = tk.Button(controls, text="⚡ Auto", command=self._toggle_autonomous,
                                     bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold"),
                                     relief="flat", padx=14, pady=8, cursor="hand2",
                                     activebackground=COLORS["panel_soft"])
        self.auto_toggle.pack(side="left", padx=4)
        tk.Button(controls, text="🔇 Silenciar", command=self.speaker.stop, bg=COLORS["panel"],
                  fg=COLORS["muted"], font=("Segoe UI", 9, "bold"), relief="flat", padx=14, pady=8,
                  cursor="hand2", activebackground=COLORS["panel_soft"]).pack(side="right")

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
            # barra de destaque na conversa ativa
            tk.Frame(row, bg=COLORS["accent"] if active else (COLORS["panel"] if active else COLORS["sidebar"]),
                     width=3).pack(side="left", fill="y")
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
        if WEBVIEW_ERROR:
            reason = WEBVIEW_ERROR.strip().splitlines()[-1] if WEBVIEW_ERROR.strip() else WEBVIEW_ERROR
            self.root.after(0, lambda r=reason: self._log(f"[UI moderna falhou] {r}", "sys"))
        if self.env_path:
            self.root.after(0, lambda: self._log(f"Config: {self.env_path}", "sys"))
        # Modo direto: fala direto com a IA (sem o backend pesado). Mais rapido e obedece.
        if self.mode == "direct":
            self.connected = True
            self.root.after(0, lambda: self._set_state("idle"))
            self.root.after(0, lambda: self._log(
                f"IA ativa ({self.llm.primary_label()}). Pasta: {self.workspace_root}", "sys"))
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
        autonomous = self.autonomous
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
        self.continuous = not self.continuous
        on = self.continuous
        self.conv_toggle.configure(
            bg=COLORS["accent"] if on else COLORS["panel"],
            fg=COLORS["bg"] if on else COLORS["muted"],
            text="💬 Conversa: ON" if on else "💬 Conversa")
        if on and self.connected and not self.busy:
            self._on_talk()

    def _toggle_autonomous(self) -> None:
        self.autonomous = not self.autonomous
        on = self.autonomous
        self.auto_toggle.configure(
            bg=COLORS["thinking"] if on else COLORS["panel"],
            fg=COLORS["bg"] if on else COLORS["muted"],
            text="⚡ Auto: ON" if on else "⚡ Auto")

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


def _find_ui_html() -> Path | None:
    for cand in (ROOT_DIR / "ui.html", Path(__file__).resolve().parent / "ui.html"):
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    return None


def _find_avatar_html() -> Path | None:
    for cand in (ROOT_DIR / "avatar.html", Path(__file__).resolve().parent / "avatar.html"):
        try:
            if cand.is_file():
                return cand
        except Exception:
            continue
    return None


def start_obs_server(api, port: int = 8777) -> int | None:
    """Sobe um servidor HTTP local leve que serve o avatar transparente (avatar.html) e o
    estado da Kemy em /state, pra usar como Browser Source no OBS. Retorna a porta usada."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    avatar = _find_avatar_html()
    if not avatar:
        return None
    try:
        page = avatar.read_bytes()
    except Exception:
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silencioso
            return

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/state", "/state/"):
                try:
                    data = json.dumps(api.obs_state()).encode("utf-8")
                except Exception:
                    data = b'{"state":"idle","mouth":0}'
                self._send(200, data, "application/json")
            else:
                self._send(200, page, "text/html; charset=utf-8")

    for p in (port, port + 1, port + 2, 0):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            real = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return real
        except Exception:
            continue
    return None


class VTubeStudio:
    """Conector com o VTube Studio via API WebSocket publica (lip-sync do modelo)."""

    def __init__(self, log, port: int = 8001) -> None:
        self.log = log
        self.port = int(port)
        self.ws = None
        self.authed = False
        self.lock = threading.Lock()
        self.token_file = config_dir() / "vts_token.txt"
        self.speaking = False
        self.mouth_provider = None
        self.on_connect = None
        self._loop_started = False
        self.mouth_param = "MouthOpen"

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        try:
            import websocket  # websocket-client
        except Exception:
            self.log("Instale 'websocket-client' para usar o VTube Studio.")
            return
        try:
            self.ws = websocket.create_connection(f"ws://127.0.0.1:{self.port}", timeout=6)
        except Exception:
            self.log("VTube Studio nao encontrado. Abra o VTS e ative 'Start API' (porta 8001).")
            return
        try:
            token = self.token_file.read_text(encoding="utf-8").strip() if self.token_file.exists() else ""
            if not token:
                r = self._send("AuthenticationTokenRequest", {"pluginName": "Kemy", "pluginDeveloper": "edul0"})
                token = (r.get("data") or {}).get("authenticationToken", "")
                if token:
                    try:
                        self.token_file.write_text(token, encoding="utf-8")
                    except Exception:
                        pass
            r = self._send("AuthenticationRequest",
                           {"pluginName": "Kemy", "pluginDeveloper": "edul0", "authenticationToken": token})
            self.authed = bool((r.get("data") or {}).get("authenticated"))
            if not self.authed:
                self.log("VTube Studio: clique em PERMITIR o plugin Kemy na janela do VTS e tente de novo.")
                return
        except Exception as exc:
            self.log(f"VTS: falha ao autenticar ({exc}).")
            return
        # Detecta o modelo e o parametro de boca real do modelo carregado.
        self._detect_mouth()
        if self.on_connect:
            try:
                self.on_connect()
            except Exception:
                pass
        try:
            self.test()  # abre/fecha a boca para o usuario ver
        except Exception:
            pass
        if not self._loop_started:
            self._loop_started = True
            threading.Thread(target=self._mouth_loop, daemon=True).start()

    def _detect_mouth(self) -> None:
        try:
            r = self._send("InputParameterListRequest", {})
            data = r.get("data") or {}
            if not data.get("modelLoaded", True):
                self.log("VTube Studio conectado, mas NENHUM modelo esta carregado. Carregue um modelo no VTS.")
                return
            params = (data.get("defaultParameters") or []) + (data.get("customParameters") or [])
            names = [p.get("name", "") for p in params]
            pick = None
            if "MouthOpen" in names:
                pick = "MouthOpen"
            else:
                pick = next((n for n in names if "mouth" in n.lower() and "open" in n.lower()), None)
                pick = pick or next((n for n in names if "mouth" in n.lower()), None)
            if pick:
                self.mouth_param = pick
            model = data.get("modelName") or "modelo"
            self.log(f"VTube Studio conectado! Modelo: {model}. Lip-sync no parametro '{self.mouth_param}'.")
        except Exception:
            self.log("VTube Studio conectado! (lip-sync em MouthOpen)")

    def test(self) -> None:
        """Abre e fecha a boca do modelo algumas vezes, para teste visual."""
        for _ in range(4):
            self.set_mouth(1.0); time.sleep(0.22)
            self.set_mouth(0.0); time.sleep(0.18)

    def _send(self, mtype: str, data: dict) -> dict:
        msg = {"apiName": "VTubeStudioPublicAPI", "apiVersion": "1.0",
               "requestID": uuid.uuid4().hex[:8], "messageType": mtype, "data": data}
        with self.lock:
            self.ws.send(json.dumps(msg))
            return json.loads(self.ws.recv())

    def set_mouth(self, value: float) -> None:
        if not (self.authed and self.ws):
            return
        try:
            self._send("InjectParameterDataRequest", {
                "faceFound": False, "mode": "set",
                "parameterValues": [{"id": self.mouth_param, "value": max(0.0, min(1.0, float(value)))}],
            })
        except Exception:
            self.authed = False

    def _mouth_loop(self) -> None:
        while True:
            try:
                if self.authed:
                    v = self.mouth_provider() if (self.speaking and self.mouth_provider) else 0.0
                    self.set_mouth(v)
            except Exception:
                pass
            time.sleep(0.04)


class WebApi:
    """Ponte JS<->Python para a UI em HTML (pywebview)."""

    def __init__(self, host: str, port: int) -> None:
        self.window = None
        self.host, self.port = host, int(port)
        self.base_url = f"http://{host}:{port}"
        self.env_path = config_dir() / ".env"
        self.env_vars = load_merged_env()
        self.workspace_root = self._workspace()
        self.llm = LLMClient(self.env_vars)
        self.mode = "direct" if self.llm.available else "online"
        self.api = LocalAPI(self.base_url if self.mode != "online" else (self.env_vars.get("ONLINE_URL") or ONLINE_URL))
        self.connected = False
        self.busy = False
        self.continuous = False
        self.autonomous = False
        self.boost = True   # ✨ Capricho: autorrevisao do codigo (qualidade nivel pro, gratis)
        self.convos_file = config_dir() / "conversations.json"
        self.convos, self.active_id = [], None
        self._load_convos()
        self.speaker = Speaker()
        self.speaker.log = lambda m: self._msg("sys", m, store=False)
        self.vts = VTubeStudio(lambda m: self._msg("sys", m, store=False))
        self.vts.mouth_provider = self.speaker.mouth_level
        self.vts.on_connect = lambda: self._js("vtsConnected()")
        self.mini = None
        self.pet_win = None
        self._pet_visible = True
        self._last_state = "idle"
        self._obs_port = None
        self._quitting = False
        self._speaking = False
        self._file_views: dict[str, dict] = {}
        self._servers: list = []   # processos de servidor de dev (Django/Flask/Node) em execucao
        self._mc_sock = None       # Minecraft (Mineflayer) — socket da ponte
        self._mc_proc = None
        self._mc_mode = False
        self._cu_running = False    # computer-use (controle do PC)
        self._cu_stop = False
        self._game_running = False
        self._game_stop = False
        self._sd_running = False    # Pokémon Showdown
        self._sd_stop = False
        self.memories = load_memorias()
        self.knowledge = load_conhecimento()
        self.speaker.on_start = self._on_speak_start
        self.speaker.on_done = self._on_speak_done
        self.listener = Listener()
        threading.Thread(target=self._mini_mouth_loop, daemon=True).start()

    def _panel(self, steps: list) -> None:
        """Mostra o painel 'ver ela trabalhar' (checklist ao vivo)."""
        try:
            self._js(f"kemyPlan({json.dumps(steps)})")
        except Exception:
            pass

    def _panel_step(self, i: int, state: str) -> None:
        try:
            self._js(f"kemyPlanStep({int(i)},{json.dumps(state)})")
        except Exception:
            pass

    def _panel_done(self) -> None:
        try:
            self._js("kemyPlanDone()")
        except Exception:
            pass

    def _on_speak_start(self) -> None:
        self.vts.speaking = True
        self._speaking = True
        self._state("speaking")

    def _on_speak_done(self) -> None:
        self.vts.speaking = False
        self._speaking = False
        self.vts.set_mouth(0.0)
        for w in (self.window, self.mini):
            try:
                if w:
                    w.evaluate_js("kemyMouth(0)")
            except Exception:
                pass
        self._after_speak()

    def _mini_mouth_loop(self) -> None:
        """Lip-sync: empurra o nivel da boca (~20fps) para o VTuber Live2D no app e a mini."""
        while True:
            try:
                if self._speaking:
                    js = f"kemyMouth({self.speaker.mouth_level():.2f})"
                    for w in (self.window, self.mini):
                        if w:
                            try:
                                w.evaluate_js(js)
                            except Exception:
                                pass
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)
            except Exception:
                time.sleep(0.1)

    # ----- helpers UI -----
    def _js(self, code: str) -> None:
        try:
            if self.window:
                self.window.evaluate_js(code)
        except Exception:
            pass

    def _state(self, s: str) -> None:
        self._last_state = s   # lido pelo overlay do OBS (/state)
        self._js(f"kemyState({json.dumps(s)})")

    def obs_state(self) -> dict:
        """Estado atual pro overlay transparente do OBS (avatar.html)."""
        sp = bool(getattr(self, "_speaking", False))
        mouth = 0.0
        if sp:
            try:
                mouth = float(self.speaker.mouth_level())
            except Exception:
                mouth = -1.0
        return {"state": getattr(self, "_last_state", "idle"), "mouth": mouth}

    # ----- janela / desktop companion -----
    def show_main(self) -> None:
        # roda numa thread para nao travar quando chamado de um clique (deadlock)
        def _do() -> None:
            try:
                if self.window:
                    self.window.show()
                    try:
                        self.window.restore()
                    except Exception:
                        pass
                    try:
                        self.window.on_top = True
                        self.window.on_top = False
                    except Exception:
                        pass
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def hide_main(self) -> None:
        def _do() -> None:
            try:
                if self.window:
                    self.window.hide()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    def quit_app(self) -> None:
        self._quitting = True
        try:
            self.speaker.stop()
        except Exception:
            pass
        try:
            import webview
            webview.destroy()
        except Exception:
            os._exit(0)

    def _msg(self, role: str, text: str, save: str | None = None, store: bool = True) -> None:
        text = strip_emojis(text)   # UI limpa/sobria, sem emojis
        self._js(f"addMsg({json.dumps(role)},{json.dumps(text)},{json.dumps(save)})")
        if store and role in ("user", "kemy"):
            it = self._cur()
            if it is not None:
                it.setdefault("log", []).append({"r": role, "t": text})
                it["log"] = it["log"][-300:]
                self._save_convos()

    def _render(self) -> None:
        pub = [{"id": c["id"], "title": c.get("title") or "Nova conversa"} for c in self.convos]
        self._js(f"renderConvos({json.dumps(pub)},{json.dumps(self.active_id)})")

    # ----- conversas -----
    def _workspace(self) -> Path:
        raw = self.env_vars.get("KEMY_LOCAL_WORKSPACE_ROOT", "").strip()
        return Path(raw) if raw else (Path.home() / "KemyWorkspace")

    def _load_convos(self) -> None:
        try:
            d = json.loads(self.convos_file.read_text(encoding="utf-8"))
            self.convos, self.active_id = d.get("items", []), d.get("active")
        except Exception:
            self.convos, self.active_id = [], None
        if not self.convos:
            self._add()
        if not self.active_id or not self._cur():
            self.active_id = self.convos[0]["id"]

    def _save_convos(self) -> None:
        try:
            self.convos_file.write_text(json.dumps({"active": self.active_id, "items": self.convos}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _add(self) -> dict:
        cid = uuid.uuid4().hex[:8]
        it = {"id": cid, "title": "Nova conversa", "session_id": None,
              "project": str(self.workspace_root / f"projeto-{cid}"), "log": []}
        self.convos.insert(0, it)
        self.active_id = cid
        self._save_convos()
        return it

    def _cur(self) -> dict | None:
        return next((c for c in self.convos if c["id"] == self.active_id), None)

    # ----- API exposta ao JS -----
    def bootstrap(self) -> dict:
        # Modo direto nao precisa de rede: ja fica pronto na hora (sem depender de
        # evaluate_js em segundo plano, que vinha falhando -> "Conectando" infinito).
        if self.mode == "direct":
            self.connected = True
        threading.Thread(target=self._connect, daemon=True).start()
        threading.Thread(target=self._update_flag, daemon=True).start()
        threading.Thread(target=self._start_obs, daemon=True).start()
        it = self._cur() or {}
        return {"state": "idle" if self.connected else "offline", "active": self.active_id,
                "convos": [{"id": c["id"], "title": c.get("title") or "Nova conversa"} for c in self.convos],
                "log": it.get("log", [])}

    def get_state(self) -> str:
        """Consultado pela UI como rede de seguranca (caso o push de estado falhe)."""
        return "idle" if self.connected else ("offline" if self.mode == "online" else "idle")

    def _start_obs(self) -> None:
        if self._obs_port:   # ja iniciado
            return
        try:
            port = int(os.environ.get("KEMY_OBS_PORT", "8777"))
        except Exception:
            port = 8777
        self._obs_port = start_obs_server(self, port)

    def obs_url(self) -> str:
        """URL do overlay transparente pro OBS (botao/menu pode mostrar/copiar)."""
        if not self._obs_port:
            return ""
        return f"http://127.0.0.1:{self._obs_port}/"

    # ---------------- Configurações (hub no app, salva no .env local) ----------------
    SETTINGS_KEYS = [
        "NVIDIA_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
        "GITHUB_MODELS_TOKEN", "MISTRAL_API_KEY", "SAMBANOVA_API_KEY", "OPENROUTER_API_KEY",
        "SUPABASE_URL", "SUPABASE_ANON_KEY", "KEMY_NETLIFY_TOKEN",
        "MC_HOST", "MC_PORT", "MC_USER", "MC_AUTH", "MC_VERSION",
    ]
    SECRET_KEYS = {"NVIDIA_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
                   "GITHUB_MODELS_TOKEN", "MISTRAL_API_KEY", "SAMBANOVA_API_KEY",
                   "OPENROUTER_API_KEY", "SUPABASE_ANON_KEY", "KEMY_NETLIFY_TOKEN"}

    def get_settings(self) -> dict:
        """Valores atuais pro hub de Configuracoes (chaves vem mascaradas por seguranca)."""
        env = getattr(self, "env_vars", {}) or {}
        out = {}
        for k in self.SETTINGS_KEYS:
            v = (env.get(k) or "").strip()
            if k in self.SECRET_KEYS and v:
                out[k] = "set:" + v[-4:]      # so mostra que existe + ultimos 4
            else:
                out[k] = v
        return out

    def save_settings(self, data) -> None:
        """Salva os campos do hub no .env LOCAL (config_dir) — sem mexer no GitHub."""
        try:
            if isinstance(data, str):
                data = json.loads(data)
        except Exception:
            data = {}
        path = config_dir() / ".env"
        changed = 0
        for k, v in (data or {}).items():
            if k not in self.SETTINGS_KEYS:
                continue
            v = ("" if v is None else str(v)).strip()
            if v.startswith("set:"):   # mascarado e nao alterado -> ignora
                continue
            if v == "":
                continue
            _set_env_var(path, k, v)
            changed += 1
        # recarrega tudo (chaves de IA, NVIDIA, Nano Banana, Minecraft…)
        self.env_path = path
        self.env_vars = load_merged_env()
        self.env_file_vars = self.env_vars
        self.llm = LLMClient(self.env_vars)
        self._msg("kemy", f"✅ Configurações salvas ({changed} campo(s))! Já apliquei — "
                  f"IA ativa: {self.llm.primary_label()}.")
        self._state("idle")

    def show_obs_url(self) -> None:
        url = self.obs_url()
        if url:
            self._msg("kemy", "🎥 **Overlay pro OBS** (fundo transparente): no OBS adicione uma "
                      f"**Fonte → Navegador** e cole esta URL:\n\n{url}\n\nDicas: marque "
                      "'Atualizar quando não visível' desligado; use `?nolabel=1` no fim da URL pra "
                      "esconder o texto de estado. A Kemy fala em sincronia automaticamente.")
        else:
            self._msg("sys", "Não consegui subir o overlay do OBS agora (avatar.html ausente?).", store=False)

    def toggle_pet(self) -> None:
        """Mostra/esconde o mascote flutuante (janela transparente sempre no topo)."""
        win = getattr(self, "pet_win", None)
        if not win:
            self._msg("sys", "O mascote flutuante não está disponível nesta versão.", store=False)
            return
        self._pet_visible = not getattr(self, "_pet_visible", True)
        vis = self._pet_visible

        def _do():
            try:
                win.show() if vis else win.hide()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()
        self._msg("sys", "🪄 Mascote flutuante " + ("ligado (arraste pra onde quiser)." if vis
                  else "escondido."), store=False)

    def _update_flag(self) -> None:
        try:
            local = (ROOT_DIR / "kemy_version.txt").read_text(encoding="utf-8").strip()
        except Exception:
            local = ""
        if not local:
            return
        try:
            req = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "KemyDesktop"})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            m = re.search(r"sha:\s*([0-9a-fA-F]{7,40})", data.get("body") or "")
            remote = m.group(1) if m else ""
            if remote and not remote.startswith(local) and not local.startswith(remote):
                self._js("setUpdate(true)")
        except Exception:
            pass

    def _connect(self) -> None:
        # O VTube Studio so conecta quando o usuario pedir (evita poluir com "nao encontrado").
        if self.mode == "direct":
            self.connected = True
            self._state("idle")
            self.speaker.say("Oi! Tô prontinha pra te ajudar.")
            return
        # online (Render)
        url = self.api.base_url
        self._msg("sys", f"Usando IA online: {url} (acordando o servidor…)", store=False)
        for _ in range(40):
            if _healthcheck(f"{url}/api/status", 4):
                break
            time.sleep(2)
        try:
            self.api.login(APP_USER, self.env_vars.get("KEMY_AUTH_PASSWORD") or APP_PASSWORD)
        except Exception:
            pass
        self.connected = True
        self._state("idle")

    def new_convo(self) -> None:
        self._add()
        self.api.session_id = None
        self._render()
        self._js("clearChat()")

    def select_convo(self, cid: str) -> None:
        if self.busy:
            return
        self.active_id = cid
        self.speaker.stop()
        it = self._cur()
        self.api.session_id = it.get("session_id") if it else None
        self._save_convos()
        self._render()
        self._js("clearChat()")
        for m in (it.get("log") if it else []) or []:
            self._msg(m.get("r", "kemy"), m.get("t", ""), store=False)

    def delete_convo(self, cid: str) -> None:
        if self.busy:
            return
        self.convos = [c for c in self.convos if c["id"] != cid]
        if not self.convos:
            self._add()
        if cid == self.active_id:
            self.active_id = self.convos[0]["id"]
        self._save_convos()
        self._render()
        self.select_convo(self.active_id)

    def toggle(self, name: str) -> None:
        if name == "conv":
            self.continuous = not self.continuous
            self._js(f"setToggle('conv',{json.dumps(self.continuous)})")
            if self.continuous:
                self._msg("sys", "💬 Modo Conversa ligado: pode falar! Eu escuto, respondo e volto a escutar sozinha.", store=False)
                if self.connected and not self.busy:
                    self.listen()
            else:
                self._msg("sys", "Modo Conversa desligado.", store=False)
        elif name == "boost":
            self.boost = not self.boost
            self._js(f"setToggle('boost',{json.dumps(self.boost)})")
            self._msg("sys", ("✨ Capricho ligado: reviso meu codigo 2x pra ficar nivel pro (um pouco mais lento)."
                              if self.boost else "Capricho desligado (mais rapido, qualidade normal)."), store=False)
        else:
            self.autonomous = not self.autonomous
            self._js(f"setToggle('auto',{json.dumps(self.autonomous)})")

    def stop_speak(self) -> None:
        self.speaker.stop()

    def open_folder(self) -> None:
        it = self._cur()
        target = Path(it["project"]) if it else self.workspace_root
        if not target.exists():
            target = self.workspace_root
        try:
            target.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception:
            pass

    def choose_workspace(self) -> None:
        """Deixa o usuario escolher a pasta onde a Kemy cria/edita os projetos."""
        try:
            res = self.window.create_file_dialog(webview_folder_dialog())  # type: ignore
        except Exception as exc:
            self._msg("sys", f"Nao consegui abrir o seletor de pasta: {exc}", store=False)
            return
        if not res:
            return
        chosen = res[0] if isinstance(res, (list, tuple)) else res
        try:
            new_root = Path(chosen)
            new_root.mkdir(parents=True, exist_ok=True)
            self.workspace_root = new_root
            self.env_vars["KEMY_LOCAL_WORKSPACE_ROOT"] = str(new_root)
            _set_env_var(config_dir() / ".env", "KEMY_LOCAL_WORKSPACE_ROOT", str(new_root))
            # projetos das proximas conversas vao para a nova pasta
            it = self._cur()
            if it:
                it["project"] = str(new_root / f"projeto-{it['id']}")
                self._save_convos()
            self._msg("sys", f"Pasta de trabalho agora e: {new_root}", store=False)
        except Exception as exc:
            self._msg("sys", f"Falha ao definir a pasta: {exc}", store=False)

    def analyze_image(self, prompt: str = "") -> None:
        # alias antigo -> agora aceita qualquer arquivo
        self.attach_file(prompt)

    def attach_file(self, prompt: str = "") -> None:
        """Anexa QUALQUER arquivo: imagem, PDF, video, Excel, CSV/texto — a Kemy le/ve e responde."""
        try:
            res = self.window.create_file_dialog(webview_open_dialog())  # type: ignore
        except Exception as exc:
            self._msg("sys", f"Nao consegui abrir o seletor: {exc}", store=False)
            return
        if not res:
            return
        path = Path(res[0] if isinstance(res, (list, tuple)) else res)
        ext = path.suffix.lower()
        self._msg("user", f"[arquivo: {path.name}] {prompt}".strip())
        self.busy = True
        self._state("thinking")
        images = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
        if ext in images:
            threading.Thread(target=self._do_vision, args=(path, images[ext], prompt), daemon=True).start()
        else:
            threading.Thread(target=self._handle_attachment, args=(path, ext, prompt), daemon=True).start()

    def _handle_attachment(self, path: Path, ext: str, prompt: str) -> None:
        """Roteia PDF/video (Gemini nativo), Excel e texto."""
        videos = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                  ".avi": "video/x-msvideo", ".mkv": "video/x-matroska", ".m4v": "video/mp4"}
        try:
            size = path.stat().st_size
            if ext == ".pdf" or ext in videos:
                if not self.llm.gemini:
                    reply = "Pra ler PDF/vídeo eu preciso da chave do Gemini configurada."
                elif size > 18 * 1024 * 1024:
                    reply = f"Esse arquivo é grande ({size//1024//1024}MB). O limite pra eu analisar direto é ~18MB."
                else:
                    mime = "application/pdf" if ext == ".pdf" else videos[ext]
                    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
                    p = prompt or ("Resuma este documento e os pontos principais." if ext == ".pdf"
                                   else "Descreva o que acontece neste vídeo.")
                    reply = self.llm.vision(p, b64, mime)
            elif ext in (".xlsx", ".xlsm"):
                data = extract_excel(path)
                reply = self.llm.chat(CHAT_PROMPT, [{"role": "user", "content":
                        f"{prompt or 'Analise esta planilha e me dê os principais insights.'}\n\n"
                        f"DADOS DA PLANILHA ({path.name}):\n{data}"}], max_tokens=2500)
            elif ext in (".csv", ".txt", ".md", ".json", ".log", ".html", ".css", ".js", ".py", ".xml", ".yml", ".ini"):
                content = path.read_text(encoding="utf-8", errors="ignore")[:15000]
                reply = self.llm.chat(CHAT_PROMPT, [{"role": "user", "content":
                        f"{prompt or 'Analise este arquivo.'}\n\nARQUIVO {path.name}:\n{content}"}], max_tokens=2500)
            else:
                reply = (f"Não sei ler o formato {ext or 'desconhecido'} ainda. Eu leio: imagem, PDF, vídeo, "
                         "Excel (.xlsx), CSV e arquivos de texto/código.")
        except Exception as exc:
            reply = f"Não consegui ler o arquivo: {exc}"
        self.busy = False
        self._msg("kemy", reply)
        if self.speaker.available and reply:
            self.speaker.say(reply[:600])
            self._state("speaking")
        else:
            self._after_speak()

    def _do_vision(self, path: Path, mime: str, prompt: str) -> None:
        # VISAO -> CODIGO: se pediu para recriar/clonar a imagem, ela GERA o site igual.
        low = (prompt or "").lower()
        build_img = (is_build_request(prompt) or any(k in low for k in
                     ("igual", "clona", "clone", "recri", "reproduz", "transforma", "vira", "monta", "copia")))
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        except Exception as exc:
            self.busy = False
            self._msg("kemy", f"Falhei ao ler a imagem: {exc}")
            self._after_speak()
            return
        if build_img:
            self._msg("sys", "🖼️→💻 Recriando a imagem em código…", store=False)
            it = self._cur()
            base = Path(it["project"]) if it else (self.workspace_root / "projeto")
            vprompt = (
                "Olhe esta imagem (um design/UI/site/tela). Recrie-a o MAIS FIEL possivel em codigo web. "
                "Responda APENAS com os arquivos no formato EXATO:\n<<<FILE: index.html>>>\n...\n<<<END>>>\n"
                "<<<FILE: styles.css>>>\n...\n<<<END>>>\n<<<FILE: script.js>>>\n...\n<<<END>>>\n"
                "Capriche: mesmas cores, layout, fontes, espacamentos e textos visiveis. Se houver imagens, "
                "use https://image.pollinations.ai/prompt/<descricao>?width=&height=&nologo=true&model=flux. "
                "Pedido extra do usuario: " + (prompt or "(recrie fielmente)"))
            try:
                reply = self.llm.vision(vprompt, b64, mime)
                if self.boost:
                    reply = self._refine(SYSTEM_PROMPT, [], "recrie a imagem em codigo", reply)
                files, chat = parse_llm_files(reply)
                self._save(files, base)
                self._localize_images(base)
                self._ensure_scripts_linked(base)
                self._open_preview(base)
                msg = chat.strip() or ("Recriei a imagem em código! Veja o preview." if files
                                       else "Não consegui extrair o código da imagem, tente de novo.")
            except Exception as exc:
                msg = f"Falhei ao recriar a imagem: {exc}"
            self.busy = False
            self._msg("kemy", msg)
            if self.speaker.available:
                self.speaker.say("Recriei a imagem em código!")
                self._state("speaking")
            else:
                self._after_speak()
            return
        try:
            reply = self.llm.vision(prompt, b64, mime)
        except Exception as exc:
            reply = f"Falhei ao ver a imagem: {exc}"
        self.busy = False
        self._msg("kemy", reply)
        if self.speaker.available and reply:
            self.speaker.say(reply[:600])
            self._state("speaking")
        else:
            self._after_speak()

    def _try_open_intent(self, text: str):
        """Abre site/app de verdade quando o usuario pede 'abra ...'. Retorna a resposta
        ou None se nao for um pedido de abrir."""
        m = re.match(r"(?i)^\s*(?:(?:pode|poderia|voc[eê]|vc|me|por\s*favor|pf|ai|a[ií])\s+)*(?:abr(?:a|ir|e)|abre|inicia[r]?|executa[r]?|p[oô]e|coloca)\s+(.+)$", text.strip())
        if not m:
            return None
        target = m.group(1).strip().strip("?.!").strip()
        target = re.sub(r"(?i)^(?:o|a|os|as|um|uma|meu|minha|o\s+app|app|site|aba)\s+", "", target).strip()
        low = target.lower()
        sites = {"youtube": "https://www.youtube.com", "google": "https://www.google.com",
                 "gmail": "https://mail.google.com", "whatsapp": "https://web.whatsapp.com",
                 "instagram": "https://www.instagram.com", "facebook": "https://www.facebook.com",
                 "twitch": "https://www.twitch.tv", "netflix": "https://www.netflix.com",
                 "github": "https://github.com", "chatgpt": "https://chatgpt.com",
                 "tiktok": "https://www.tiktok.com", "twitter": "https://x.com", "x": "https://x.com"}
        apps = {"notepad": "notepad", "bloco de notas": "notepad", "calculadora": "calc", "calc": "calc",
                "paint": "mspaint", "explorador": "explorer", "explorador de arquivos": "explorer",
                "cmd": "cmd", "terminal": "cmd", "spotify": "spotify", "discord": "discord",
                "steam": "steam", "chrome": "chrome", "edge": "msedge", "configuracoes": "ms-settings:"}
        cmd, label = None, target
        ym = re.search(r"(?i)(?:canal|v[ií]deo|m[uú]sica|playlist|live)?\s*(.+?)\s+n[oa]\s+youtube", low)
        if "youtube" in low and ym and ym.group(1).strip() not in ("", "o", "a"):
            q = ym.group(1).strip()
            cmd = f'start "" "https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"'
            label = f"{q} no YouTube"
        elif low in sites:
            cmd = f'start "" "{sites[low]}"'
        elif low in apps:
            a = apps[low]
            cmd = f'start "" "{a}"' if a.startswith(("http", "ms-")) else f"start {a}"
        elif low.startswith(("http://", "https://")) or re.match(r"^[\w-]+\.\w{2,}", low):
            url = target if low.startswith("http") else "https://" + target
            cmd = f'start "" "{url}"'
        else:
            cmd = f'start "" "https://www.google.com/search?q={urllib.parse.quote(target)}"'
            label = f"uma busca por '{target}'"
        try:
            subprocess.Popen(cmd, shell=True)
            return f"Pronto, abri {label}! 👍"
        except Exception as exc:
            return f"Tentei abrir {label} mas deu erro: {exc}"

    def _screen_reply(self, prompt: str) -> str:
        """Tira print da tela e a Kemy analisa (visao). Minimiza a janela antes,
        para capturar o que esta ATRAS do Kemy."""
        if not self.llm.gemini:
            return "Pra ver sua tela eu preciso da chave do Gemini configurada."
        self._msg("sys", "👁️ Olhando sua tela…", store=False)
        try:
            import io
            from PIL import ImageGrab
            # NAO usar window.minimize()/restore() aqui: operacao de janela vinda de uma
            # thread trava o app (mesmo deadlock do modo Mini). Capturo a tela como esta.
            time.sleep(0.2)
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            return self.llm.vision(prompt + "\n(Esta e a tela atual do usuario.)", b64, "image/jpeg")
        except Exception as exc:
            return f"Nao consegui capturar a tela: {exc}"

    def see_screen(self) -> None:
        """Botao: a Kemy olha a tela e comenta."""
        if self.busy:
            return
        self._msg("user", "👁️ (olha minha tela)", store=False)
        self.busy = True
        self._state("thinking")

        def _run() -> None:
            reply = self._screen_reply("Descreva o que estou vendo na minha tela e, se fizer sentido, me ajude.")
            self.busy = False
            self._msg("kemy", reply)
            if self.speaker.available and reply:
                self.speaker.say(reply[:600])
                self._state("speaking")
            else:
                self._after_speak()

        threading.Thread(target=_run, daemon=True).start()

    # ---------------- MODO JOGO (agente que joga por visão) ----------------
    def _key_presser(self):
        """Retorna uma funcao press(key, hold) que envia teclas pro jogo. Prefere
        pydirectinput (funciona em jogos/emuladores); cai pro pyautogui."""
        try:
            import pydirectinput as pdi
            pdi.FAILSAFE = False
            pdi.PAUSE = 0.04

            def press(k, hold=0.06):
                try:
                    pdi.keyDown(k); time.sleep(max(0.03, hold)); pdi.keyUp(k)
                except Exception:
                    pass
            return press
        except Exception:
            pass
        try:
            import pyautogui as pg
            pg.FAILSAFE = False

            def press(k, hold=0.06):
                try:
                    pg.keyDown(k); time.sleep(max(0.03, hold)); pg.keyUp(k)
                except Exception:
                    pass
            return press
        except Exception:
            return None

    def play_game(self, goal: str = "") -> None:
        """Modo jogo: a Kemy olha a tela, entende o jogo e joga sozinha (loop visão→tecla)."""
        if not self.llm.gemini:
            self._msg("kemy", "Pra jogar eu preciso enxergar a tela — configure a chave do Gemini (GEMINI_API_KEY).")
            return
        if getattr(self, "_game_running", False):
            self._msg("sys", "Já estou jogando. Diga 'parar jogo' pra eu parar.", store=False)
            return
        if not self._key_presser():
            self._msg("kemy", "Pra apertar as teclas do jogo eu preciso da biblioteca pydirectinput. "
                      "No modo Auto eu instalo: pip install pydirectinput")
            return
        self._game_running = True
        self._game_stop = False
        goal = (goal or "").strip() or "avançar no objetivo principal do jogo"
        self._msg("kemy", f"🎮 Modo jogo ligado! Objetivo: **{goal}**.\nClique na janela do jogo pra deixar "
                  "ela em foco. Diga ou digite **'parar jogo'** quando quiser que eu pare.")
        threading.Thread(target=self._game_loop, args=(goal,), daemon=True).start()

    def stop_game(self) -> None:
        if getattr(self, "_game_running", False):
            self._game_stop = True
            self._msg("sys", "🎮 Parando o modo jogo…", store=False)

    def _read_emu_state(self) -> dict:
        """Lê o estado do jogo gravado pelo bridge Lua do mGBA (memória). Vazio se não houver."""
        try:
            p = Path(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp") / "kemy_pokemon.json"
            if p.exists() and (time.time() - p.stat().st_mtime) < 10:
                return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            pass
        return {}

    def _game_loop(self, goal: str, max_steps: int = 600) -> None:
        import io
        from PIL import ImageGrab
        press = self._key_presser()
        self._state("thinking")
        is_pkmn = "pok" in (goal or "").lower()
        hist: list[str] = []
        prompt_base = (
            "Voce e uma IA que JOGA videogame olhando a tela. OBJETIVO: " + goal + ".\n"
            "Mapeamento tipico de emulador (GBA/SNES): setas = direcao; z = A (confirmar/avancar texto); "
            "x = B (voltar/cancelar); enter = Start; backspace = Select. Em outros jogos use as teclas "
            "obvias (wasd/setas/espaco/e).\n"
            "Olhe o estado atual e decida as PROXIMAS teclas. Responda SO um JSON: "
            '{\"reason\":\"o que esta vendo e o plano em 1 frase\",\"keys\":[\"z\"],\"hold\":0.06,\"done\":false}. '
            "keys = lista de teclas a apertar em sequencia (1 a 4). Para andar bastante, repita a tecla. "
            "done=true so quando o objetivo for cumprido.")
        for step in range(max_steps):
            if self._game_stop:
                break
            try:
                img = ImageGrab.grab()
                img.thumbnail((900, 600))  # menor = mais rapido/barato
                buf = io.BytesIO(); img.convert("RGB").save(buf, format="JPEG", quality=65)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                time.sleep(0.6); continue
            ctx = prompt_base + ("\n\nUltimas acoes: " + " | ".join(hist[-5:]) if hist else "")
            if is_pkmn:
                mem = self._read_emu_state()
                if mem:
                    ctx += ("\n\nESTADO DA MEMORIA (use pra decidir com estrategia — cure se HP baixo, "
                            "evite lutar perdendo): " + json.dumps(mem))
            try:
                out = self.llm.vision(ctx, b64, "image/jpeg")
            except Exception as e:
                self._msg("sys", f"(visão falhou: {e})", store=False); time.sleep(1.0); continue
            action = self._parse_game_action(out)
            reason = (action.get("reason") or "").strip()
            if reason:
                self._msg("sys", f"🎮 {reason}", store=False)
                hist.append(reason[:60])
            if action.get("done"):
                self._msg("kemy", "🏆 Cheguei no objetivo! (ou foi o que entendi). Parando.")
                break
            keys = action.get("keys") or []
            hold = action.get("hold", 0.06)
            for k in keys[:4]:
                if self._game_stop:
                    break
                kk = self._norm_key(k)
                if kk and press:
                    press(kk, hold)
                    time.sleep(0.12)
            time.sleep(0.5)
        self._game_running = False
        self._state("idle")
        if not self._game_stop:
            self._msg("kemy", "Parei o modo jogo (limite de passos). É só pedir de novo. 🎮")
        else:
            self._msg("kemy", "Parei o jogo. 🎮")

    def _parse_game_action(self, out: str) -> dict:
        try:
            m = re.search(r"\{.*\}", out or "", re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
        # fallback: tenta achar teclas mencionadas
        keys = re.findall(r"(?i)\b(up|down|left|right|cima|baixo|esquerda|direita|z|x|enter|space|a|b|w|s|d|e)\b", out or "")
        return {"reason": (out or "")[:80], "keys": keys[:3], "done": "done" in (out or "").lower()}

    def _norm_key(self, k: str) -> str:
        k = (k or "").strip().lower()
        tr = {"cima": "up", "baixo": "down", "esquerda": "left", "direita": "right",
              "espaco": "space", "espaço": "space", "start": "enter", "select": "backspace"}
        return tr.get(k, k)

    # ---------------- POKÉMON SHOWDOWN (batalha online via protocolo) ----------------
    def showdown_play(self, fmt: str = "gen9randombattle") -> None:
        """A Kemy batalha no Pokémon Showdown (random battle): a IA escolhe os golpes."""
        if getattr(self, "_sd_running", False):
            self._msg("sys", "Já estou no Showdown. Diga 'parar' pra sair.", store=False)
            return
        try:
            import websocket  # noqa: F401
        except Exception:
            self._msg("kemy", "Pra batalhar no Showdown eu preciso da lib websocket-client.")
            return
        self._sd_running = True
        self._sd_stop = False
        self._msg("kemy", "⚔️ Entrando no Pokémon Showdown e procurando uma batalha (random)… diga 'parar' pra sair.")
        threading.Thread(target=self._sd_loop, args=(fmt,), daemon=True).start()

    def stop_showdown(self) -> None:
        if getattr(self, "_sd_running", False):
            self._sd_stop = True
            self._msg("sys", "⚔️ Saindo do Showdown…", store=False)

    def _sd_login(self, challstr: str, name: str) -> str:
        """Pega a 'assertion' de convidado (sem senha) pra logar no Showdown."""
        try:
            userid = re.sub(r"[^a-z0-9]", "", name.lower())
            data = urllib.parse.urlencode({"act": "getassertion", "userid": userid,
                                           "challstr": challstr}).encode("utf-8")
            req = urllib.request.Request("https://play.pokemonshowdown.com/action.php", data=data,
                                         headers={"User-Agent": BROWSER_UA})
            return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore").strip()
        except Exception:
            return ""

    def _sd_choose(self, room: str, request: dict, log_tail: str) -> str:
        """Decide a jogada (golpe/troca) com a IA; cai pra 'default' se algo falhar."""
        try:
            if request.get("forceSwitch"):
                opts = []
                for i, p in enumerate(request.get("side", {}).get("pokemon", []), 1):
                    if not p.get("active") and not p.get("condition", "").endswith(" fnt"):
                        opts.append(f"switch {i} ({p.get('ident','')})")
                prompt = ("Voce DEVE trocar de pokémon. Opcoes: " + "; ".join(opts) +
                          ". Responda SO JSON {\"choice\":\"switch N\"}.")
                payload = json.dumps({"forceSwitch": True, "options": opts})
            else:
                active = (request.get("active") or [{}])[0]
                moves = active.get("moves", [])
                lst = [f"{i}: {m.get('move')} (tipo via nome, pp {m.get('pp')}, {'OFF' if m.get('disabled') else 'ok'})"
                       for i, m in enumerate(moves, 1)]
                team = [f"{p.get('ident','')} {p.get('condition','')}" for p in request.get("side", {}).get("pokemon", [])]
                prompt = ("Escolha o MELHOR golpe pra ganhar (pense em vantagem de tipo e dano). "
                          "Golpes: " + " | ".join(lst) + ". Seu time: " + "; ".join(team) +
                          ". Responda SO JSON {\"choice\":\"move N\"} (ou \"switch N\" se for melhor trocar).")
                payload = json.dumps({"moves": lst})
            out = self.llm.chat("Voce e uma jogadora competitiva de Pokémon. " + prompt,
                                [{"role": "user", "content": "Estado recente:\n" + log_tail[-800:] + "\n" + payload}],
                                max_tokens=40, fast=True)
            m = re.search(r"\{.*\}", out or "", re.DOTALL)
            ch = (json.loads(m.group(0)).get("choice") if m else "") or ""
            ch = ch.strip().lower()
            if re.match(r"(move|switch)\s+\d+", ch):
                return ch
        except Exception:
            pass
        return "default"

    def _sd_loop(self, fmt: str) -> None:
        import websocket
        name = "Kemy" + str(random.randint(100, 999))
        try:
            ws = websocket.create_connection("wss://sim3.psim.us/showdown/websocket", timeout=30)
        except Exception as e:
            self._sd_running = False
            self._msg("kemy", f"Não consegui conectar no Showdown: {e}")
            return
        rooms: dict = {}
        searched = False
        try:
            while not self._sd_stop:
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    continue
                room = ""
                if raw.startswith(">"):
                    nl = raw.find("\n"); room = raw[1:nl] if nl > 0 else raw[1:]; raw = raw[nl + 1:] if nl > 0 else ""
                for line in raw.split("\n"):
                    if not line.startswith("|"):
                        continue
                    parts = line.split("|")
                    cmd = parts[1] if len(parts) > 1 else ""
                    if cmd == "challstr":
                        challstr = "|".join(parts[2:])
                        assertion = self._sd_login(challstr, name)
                        if assertion:
                            ws.send(f"|/trn {name},0,{assertion}")
                    elif cmd == "updateuser" and not searched and len(parts) > 2 and not parts[2].strip().startswith("Guest"):
                        searched = True
                        ws.send(f"|/search {fmt}")
                        self._msg("sys", f"⚔️ Logada como {name}, procurando partida ({fmt})…", store=False)
                    elif cmd == "request" and room:
                        try:
                            req = json.loads(parts[2]) if parts[2].strip() else {}
                        except Exception:
                            req = {}
                        if req and (req.get("active") or req.get("forceSwitch")):
                            rooms.setdefault(room, "")
                            choice = self._sd_choose(room, req, rooms.get(room, ""))
                            rqid = req.get("rqid", "")
                            ws.send(f"{room}|/choose {choice}|{rqid}")
                            self._msg("sys", f"⚔️ Jogada: {choice}", store=False)
                    elif cmd in ("win", "tie"):
                        who = parts[2] if len(parts) > 2 else ""
                        won = (cmd == "win" and who == name)
                        self._msg("kemy", "🏆 Ganhei a batalha!" if won else ("🤝 Empate." if cmd == "tie" else f"😅 Perdi pra {who}. Bora de novo?"))
                        if not self._sd_stop:
                            ws.send(f"|/search {fmt}")
                    elif cmd in ("error", "popup"):
                        self._msg("sys", f"⚔️ {' '.join(parts[2:])[:160]}", store=False)
                    if room:
                        rooms[room] = (rooms.get(room, "") + "\n" + line)[-2000:]
        finally:
            try:
                ws.close()
            except Exception:
                pass
            self._sd_running = False
            self._state("idle")
            self._msg("kemy", "⚔️ Saí do Showdown.")

    # ---------------- COMPUTER-USE (opera o PC/navegador por visão) ----------------
    def computer_use(self, goal: str = "") -> None:
        """A Kemy opera o PC: tira print, decide a ação (clicar/digitar/rolar) e executa."""
        if not self.llm.gemini:
            self._msg("kemy", "Pra controlar o PC eu preciso enxergar a tela — configure a chave do Gemini.")
            return
        if getattr(self, "_cu_running", False):
            self._msg("sys", "Já estou usando o PC. Diga 'parar' pra eu parar.", store=False)
            return
        try:
            import pyautogui  # noqa: F401
        except Exception:
            self._msg("kemy", "Pra controlar o PC eu preciso do pyautogui. No modo Auto: pip install pyautogui")
            return
        goal = (goal or "").strip()
        if not goal:
            self._msg("kemy", "Me diz o que fazer no PC. Ex.: 'pesquise no Google por notebooks e abra o primeiro'.")
            return
        self._cu_running = True
        self._cu_stop = False
        self._msg("kemy", f"🖱️ Tô no controle! Objetivo: **{goal}**. Pra eu parar na hora, diga **'parar'** "
                  "ou jogue o mouse pro canto superior-esquerdo da tela.")
        threading.Thread(target=self._cu_loop, args=(goal,), daemon=True).start()

    def stop_computer(self) -> None:
        if getattr(self, "_cu_running", False):
            self._cu_stop = True
            self._msg("sys", "🖱️ Parando o controle do PC…", store=False)

    def _cu_loop(self, goal: str, max_steps: int = 40) -> None:
        import io
        from PIL import ImageGrab
        try:
            import pyautogui
            pyautogui.FAILSAFE = True  # mouse no canto sup-esq aborta
        except Exception:
            self._cu_running = False
            return
        self._state("thinking")
        hist: list[str] = []
        base_prompt = (
            "Voce CONTROLA o computador olhando a tela pra cumprir o OBJETIVO: " + goal + ".\n"
            "A tela e um plano de coordenadas NORMALIZADAS de 0 a 1000 (x=0 esquerda,1000 direita; "
            "y=0 topo,1000 base). Decida a PROXIMA acao e responda SO um JSON:\n"
            '{\"reason\":\"o que ve e o plano em 1 frase\",\"action\":\"click|double_click|right_click|'
            'move|type|key|scroll|open_url|wait|done\",\"x\":500,\"y\":500,\"text\":\"...\",'
            '\"keys\":[\"enter\"],\"amount\":-400,\"url\":\"https://...\"}\n'
            "Use 'open_url' pra abrir um site direto no navegador. 'type' digita um texto; 'key' aperta "
            "teclas (ex.: enter, ctrl+a). 'scroll' usa amount (negativo desce). done=true quando concluir.")
        for step in range(max_steps):
            if self._cu_stop:
                break
            try:
                full = ImageGrab.grab()
                W, H = full.size
                small = full.copy(); small.thumbnail((1100, 700))
                buf = io.BytesIO(); small.convert("RGB").save(buf, format="JPEG", quality=70)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception:
                time.sleep(0.8); continue
            ctx = base_prompt + ("\n\nUltimas acoes: " + " | ".join(hist[-5:]) if hist else "")
            try:
                out = self.llm.vision(ctx, b64, "image/jpeg")
            except Exception as e:
                self._msg("sys", f"(visão falhou: {e})", store=False); time.sleep(1.0); continue
            act = self._parse_game_action(out)
            reason = (act.get("reason") or "").strip()
            if reason:
                self._msg("sys", f"🖱️ {reason}", store=False); hist.append(reason[:60])
            a = (act.get("action") or "").lower()
            if a == "done" or act.get("done"):
                self._msg("kemy", "✅ Acho que terminei! Confere aí.")
                break
            try:
                px = int(float(act.get("x", 500)) / 1000.0 * W)
                py = int(float(act.get("y", 500)) / 1000.0 * H)
            except Exception:
                px, py = W // 2, H // 2
            try:
                if a in ("click", "double_click", "right_click", "move"):
                    pyautogui.moveTo(px, py, duration=0.3)
                    if a == "click":
                        pyautogui.click()
                    elif a == "double_click":
                        pyautogui.doubleClick()
                    elif a == "right_click":
                        pyautogui.rightClick()
                elif a == "type":
                    pyautogui.write(str(act.get("text", "")), interval=0.02)
                elif a == "key":
                    keys = act.get("keys") or []
                    if len(keys) > 1:
                        pyautogui.hotkey(*[self._norm_key(k) for k in keys])
                    elif keys:
                        pyautogui.press(self._norm_key(keys[0]))
                elif a == "scroll":
                    pyautogui.scroll(int(act.get("amount", -400)))
                elif a == "open_url":
                    url = act.get("url") or ""
                    if url:
                        webbrowser.open(url); time.sleep(2.0)
                elif a == "wait":
                    time.sleep(1.0)
            except pyautogui.FailSafeException:
                self._msg("kemy", "🛑 Você jogou o mouse no canto — parei na hora!")
                break
            except Exception as e:
                self._msg("sys", f"(ação falhou: {e})", store=False)
            time.sleep(0.7)
        self._cu_running = False
        self._state("idle")
        if not self._cu_stop:
            self._msg("kemy", "Parei (limite de passos). Me diz se ficou bom ou o que ajustar. 🖱️")
        else:
            self._msg("kemy", "Parei o controle do PC. 🖱️")

    # ---------------- MINECRAFT (player inteligente via Mineflayer) ----------------
    def _find_minecraft_dir(self) -> Path | None:
        for cand in (ROOT_DIR / "minecraft", Path(__file__).resolve().parent / "minecraft"):
            try:
                if (cand / "kemy_bot.js").is_file():
                    return cand
            except Exception:
                continue
        return None

    def mc_start(self) -> None:
        """Conecta a Kemy no Minecraft (sobe o bot Mineflayer e liga a ponte)."""
        if getattr(self, "_mc_sock", None):
            self._msg("sys", "Já estou no Minecraft. Diga 'sai do minecraft' pra sair.", store=False)
            return
        mdir = self._find_minecraft_dir()
        if not mdir:
            self._msg("kemy", "Não achei o módulo do Minecraft no app.")
            return
        node = None
        for exe in ("node", "node.exe"):
            try:
                subprocess.run([exe, "--version"], capture_output=True, timeout=8, **proc_quiet()); node = exe; break
            except Exception:
                continue
        if not node:
            self._msg("kemy", "Pra jogar Minecraft eu preciso do Node.js instalado. No modo Auto eu instalo: "
                      "winget install -e --id OpenJS.NodeJS")
            return
        ev = dict(os.environ)
        for k in ("MC_HOST", "MC_PORT", "MC_USER", "MC_AUTH", "MC_VERSION"):
            v = (self.env_vars.get(k) if hasattr(self, "env_vars") else None)
            if v:
                ev[k] = v
        port = ev.get("KEMY_MC_PORT", "8079"); ev["KEMY_MC_PORT"] = port
        host = ev.get("MC_HOST", "localhost")
        self._msg("kemy", f"🎮 Entrando no Minecraft ({host})… (na 1ª vez instalo as libs, demora um pouco)")
        self._mc_resp = __import__("queue").Queue()

        def boot():
            try:
                if not (mdir / "node_modules").exists():
                    self._msg("sys", "📦 Instalando libs do Minecraft (mineflayer)…", store=False)
                    subprocess.run([node.replace("node", "npm"), "install"], cwd=str(mdir),
                                   capture_output=True, timeout=600, **proc_quiet())
                log = open(mdir / "_mc.log", "wb")
                self._mc_proc = subprocess.Popen([node, "kemy_bot.js"], cwd=str(mdir), env=ev,
                                                 stdout=log, stderr=subprocess.STDOUT, **proc_quiet())
                time.sleep(2.5)
                import socket
                for _ in range(20):
                    try:
                        s = socket.create_connection(("127.0.0.1", int(port)), timeout=2)
                        self._mc_sock = s
                        threading.Thread(target=self._mc_reader, daemon=True).start()
                        return
                    except Exception:
                        time.sleep(0.6)
                self._msg("kemy", "Não consegui falar com o bot do Minecraft. Veja o _mc.log na pasta minecraft.")
            except Exception as e:
                self._msg("kemy", f"Falha ao iniciar o Minecraft: {e}")
        threading.Thread(target=boot, daemon=True).start()

    def mc_stop(self) -> None:
        self._mc_mode = False
        try:
            if getattr(self, "_mc_sock", None):
                self._mc_sock.close()
        except Exception:
            pass
        self._mc_sock = None
        try:
            if getattr(self, "_mc_proc", None):
                self._mc_proc.terminate()
        except Exception:
            pass
        self._mc_proc = None
        self._msg("sys", "🎮 Saí do Minecraft.", store=False)

    def _mc_reader(self) -> None:
        buf = ""
        sock = self._mc_sock
        while sock and sock is self._mc_sock:
            try:
                data = sock.recv(4096)
            except Exception:
                break
            if not data:
                break
            buf += data.decode("utf-8", "ignore")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if "ok" in obj or "state" in obj and "event" not in obj:
                    try:
                        self._mc_resp.put_nowait(obj)
                    except Exception:
                        pass
                ev = obj.get("event")
                if ev == "ready":
                    self._mc_mode = True
                    self._msg("kemy", "✅ Entrei no Minecraft! Pode falar comigo normalmente — "
                              "'vem cá', 'minera 5 ferro', 'constrói uma casa', 'me defende'. 'Sai do minecraft' pra sair.")
                elif ev == "chat":
                    self._mc_on_chat(obj.get("user", ""), obj.get("text", ""))
                elif ev == "death":
                    self._msg("kemy", "💀 Morri no jogo! Já volto.")
                elif ev in ("kicked", "error", "end"):
                    self._msg("sys", f"🎮 {obj.get('msg', ev)}", store=False)

    def mc_send(self, obj: dict, timeout: float = 30) -> dict:
        sock = getattr(self, "_mc_sock", None)
        if not sock:
            return {"ok": False, "msg": "não estou no Minecraft"}
        try:
            sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception as e:
            return {"ok": False, "msg": str(e)}
        try:
            return self._mc_resp.get(timeout=timeout)
        except Exception:
            return {"ok": True, "msg": "(executando…)"}

    def _mc_on_chat(self, user: str, text: str) -> None:
        """Alguém falou no chat do jogo — a Kemy responde/age se foi com ela."""
        low = (text or "").lower()
        self._msg("sys", f"💬 [{user}] {text}", store=False)
        if "kemy" in low or low.startswith(("!", "@")):
            threading.Thread(target=self.mc_brain, args=(text, user), daemon=True).start()

    def mc_brain(self, request: str, who: str = "") -> None:
        """O cérebro: lê o estado do jogo + o pedido e decide as ações (skills) a executar."""
        if not getattr(self, "_mc_sock", None):
            return
        st = self.mc_send({"cmd": "state"}, timeout=10).get("state", {})
        sysp = (
            "Voce e a Kemy jogando Minecraft como uma jogadora inteligente e simpatica. Recebe o ESTADO do "
            "jogo e um PEDIDO, e decide as ACOES. Skills disponiveis (responda SO um JSON):\n"
            '{"reply":"resposta curta e natural em PT-BR pro jogador","actions":[ ... ]}\n'
            "Cada acao e um objeto: {\"cmd\":\"come\"} (vir ate o jogador), {\"cmd\":\"follow\",\"name\":\"Player\"}, "
            "{\"cmd\":\"stop\"}, {\"cmd\":\"goto\",\"x\":..,\"y\":..,\"z\":..}, "
            "{\"cmd\":\"mine\",\"name\":\"iron_ore\",\"count\":5}, {\"cmd\":\"collect\"}, "
            "{\"cmd\":\"attack\"}, {\"cmd\":\"place\",\"name\":\"oak_planks\"}, {\"cmd\":\"say\",\"text\":\"oi\"}. "
            "Use nomes de bloco do Minecraft em ingles. Seja proativa e natural, nao robotica.")
        usr = f"ESTADO:\n{json.dumps(st, ensure_ascii=False)}\n\nPEDIDO de {who or 'jogador'}: {request}"
        try:
            out = self.llm.chat(sysp, [{"role": "user", "content": usr}], max_tokens=600, fast=True)
            m = re.search(r"\{.*\}", out or "", re.DOTALL)
            plan = json.loads(m.group(0)) if m else {}
        except Exception:
            plan = {}
        reply = (plan.get("reply") or "").strip()
        if reply:
            self._msg("kemy", reply)
            self.mc_send({"cmd": "say", "text": reply[:200]}, timeout=5)
            if self.speaker.available:
                self.speaker.say(reply[:200])
        for act in (plan.get("actions") or [])[:8]:
            if not getattr(self, "_mc_sock", None):
                break
            r = self.mc_send(act, timeout=60)
            if r.get("msg"):
                self._msg("sys", f"🎮 {r['msg']}", store=False)

    def toggle_overlay(self) -> None:
        """Modo mini foi REMOVIDO (era a principal causa de travamento). Para overlay de
        stream, use o OBS: Window Capture na janela da Kemy (recorte no avatar)."""
        self._msg("sys", "O modo Mini foi removido (deixava o app travado). Pra stream, "
                  "use o OBS → Window Capture na janela da Kemy e recorte no avatar. 🎥", store=False)

    def preview(self) -> None:
        """Abre o preview do site da conversa atual no navegador."""
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        if not (base / "index.html").exists():
            self._msg("sys", "Ainda nao ha um site para visualizar nesta conversa.", store=False)
            return
        self._open_preview(base)

    def open_preview_external(self) -> None:
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        idx = base / "index.html"
        if idx.exists():
            try:
                webbrowser.open(idx.as_uri())
            except Exception:
                pass

    def deploy_site(self) -> None:
        """Publica o site (index.html) num link público grátis (Netlify)."""
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        if not (base / "index.html").exists():
            self._msg("sys", "Não há um site (index.html) pra publicar nesta conversa.", store=False)
            self._state("idle")
            return
        token = (self.env_vars.get("KEMY_NETLIFY_TOKEN") or self.env_vars.get("NETLIFY_TOKEN") or "").strip()
        if not token:
            self._msg("kemy", "Pra publicar de graça eu uso o Netlify. Crie uma conta grátis em netlify.com → "
                      "User settings → Applications → 'New access token' → copie. Depois me mande pelo .env: "
                      "KEMY_NETLIFY_TOKEN=seu_token (ou pelo botão de config). Aí eu publico com 1 clique! 🚀")
            self._state("idle")
            return
        self.busy = True
        self._state("thinking")
        threading.Thread(target=self._do_deploy, args=(base, token), daemon=True).start()

    def _do_deploy(self, base: Path, token: str) -> None:
        try:
            self._msg("sys", "🚀 Publicando o site no Netlify…", store=False)
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in base.rglob("*"):
                    if p.is_file() and not p.name.startswith("_") and "_bg_" not in p.name and p.suffix.lower() != ".pdf":
                        zf.write(p, p.relative_to(base).as_posix())
            zipdata = buf.getvalue()
            req = urllib.request.Request("https://api.netlify.com/api/v1/sites", data=b"{}", method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            site = json.loads(urllib.request.urlopen(req, timeout=40).read().decode("utf-8"))
            site_id = site["id"]
            url = site.get("ssl_url") or site.get("url") or ""
            req2 = urllib.request.Request(f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
                                          data=zipdata, method="POST",
                                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/zip"})
            urllib.request.urlopen(req2, timeout=180).read()
            self.busy = False
            self._msg("kemy", f"🚀 Pronto! Seu site está NO AR em:\n{url}\n\nÉ só compartilhar esse link.")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            self._after_speak()
        except Exception as exc:
            self.busy = False
            self._msg("kemy", f"Não consegui publicar: {exc}. Confere se o token do Netlify está certo.")
            self._after_speak()

    def import_env(self) -> None:
        try:
            res = self.window.create_file_dialog(webview_open_dialog())  # type: ignore
        except Exception:
            res = None
        if not res:
            return
        src = res[0] if isinstance(res, (list, tuple)) else res
        try:
            shutil.copyfile(src, config_dir() / ".env")
        except Exception as exc:
            self._msg("sys", f"Falha ao salvar .env: {exc}", store=False)
            return
        self.env_path = config_dir() / ".env"
        self.env_vars = load_merged_env()
        self.workspace_root = self._workspace()
        self.llm = LLMClient(self.env_vars)
        self.mode = "direct" if self.llm.available else "online"
        self._msg("sys", "Config salva. IA reconfigurada.", store=False)
        threading.Thread(target=self._connect, daemon=True).start()

    def listen(self) -> None:
        if not self.listener.available:
            self._msg("sys", "🎤 Microfone indisponivel neste PC (instale o PyAudio ou verifique o microfone).", store=False)
            return
        if not self.connected:
            self._msg("sys", "Espera conectar antes de falar…", store=False)
            return
        if self.busy:
            self._msg("sys", "Deixa eu terminar de responder primeiro 😊", store=False)
            return
        self.speaker.stop()
        self.listener.listen_once(
            on_state=lambda s: self._state(s),
            on_text=lambda t: self._handle(t),
            on_error=self._on_listen_error,
        )

    def _on_listen_error(self, e: str) -> None:
        self._state("idle")
        low = (e or "").lower()
        no_mic = ("input device" in low or "no default" in low or "indisponivel" in low
                  or "microfone" in low)
        if no_mic:
            # Sem microfone: nao adianta insistir (evita spam no modo Conversa).
            if self.continuous:
                self.continuous = False
                self._js("setToggle('conv',false)")
            self._msg("sys", "🎤 Nenhum microfone disponivel neste PC. Desliguei a Conversa — pode digitar normalmente. (Conecte um microfone e defina como padrao no Windows para falar.)", store=False)
            return
        self._msg("sys", f"🎤 {e}", store=False)
        self._relisten_if_conv()

    def _relisten_if_conv(self) -> None:
        # No modo Conversa, se nao ouviu nada, tenta de novo automaticamente.
        if self.continuous and self.connected and not self.busy:
            threading.Timer(0.6, self.listen).start()

    def send_text(self, text: str) -> None:
        self._handle(text)

    # ----- nucleo -----
    def _handle(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            self._state("idle")
            return
        it = self._cur()
        if it is not None and (it.get("title") in (None, "", "Nova conversa")):
            it["title"] = text[:40]
            self._render()
        self._msg("user", text)
        if self._maybe_learn(text):   # "lembre que ...", "de agora em diante ..."
            self._state("idle")
            return
        if self._app_command(text):   # comandos de controle do app (voz ou texto)
            return
        # No Minecraft: a fala vira ação no jogo (cérebro do bot).
        if getattr(self, "_mc_mode", False) and getattr(self, "_mc_sock", None):
            threading.Thread(target=self.mc_brain, args=(text, "você"), daemon=True).start()
            self._state("idle")
            return
        played = self._try_play_intent(text)   # "toque <musica>" -> toca no YouTube
        if played is not None:
            self._msg("kemy", played)
            if self.speaker.available:
                self.speaker.say(played[:200]); self._state("speaking")
            else:
                self._state("idle")
            return
        if not self.connected:
            self._msg("sys", "Ainda conectando…", store=False)
            return
        if self._maybe_learn_topic(text):   # "aprenda sobre X / estude X" -> RAG de conhecimento
            return
        if re.match(r"(?i)^(?:desfaz|desfaça|desfaca|desfazer|undo|volta[r]? (?:a|pra|para) (?:versao|versão) anterior|volta atras|volta atrás)\b", text.strip()):
            self.git_undo()
            self._state("idle")
            return
        if re.match(r"(?i)^(?:publi(?:que|car|ca)|deploy|hosped[ae]|coloca[r]? (?:no ar|online)|sobe[r]? o site|p[oõ]e[r]? (?:no ar|online)|coloca[r]? online)\b", text.strip()):
            self.deploy_site()
            return
        self.busy = True
        self._state("thinking")
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _maybe_learn(self, text: str) -> bool:
        """Aprende quando o usuario ensina/corrige (memoria persistente)."""
        t = (text or "").strip()
        m = re.match(r"(?i)^(?:lembr[ae]|anota|anote|guarda|memoriza|grava)\s+(?:disso[:,]?\s*|que\s+)?(.+)$", t)
        if not m:
            m = re.match(r"(?i)^(?:de agora em diante|da proxima vez|a partir de agora|sempre que voce|toda vez)\b[,:]?\s*(.+)$", t)
        if not m:
            return False
        lesson = m.group(1).strip().rstrip(".!").strip()
        if len(lesson) < 3:
            return False
        if lesson not in self.memories:
            self.memories.append(lesson)
            save_memorias(self.memories)
        self._msg("kemy", f"Anotado! 🧠 Vou lembrar disso: \"{lesson}\".")
        if self.speaker.available:
            self.speaker.say("Anotado! Vou lembrar disso.")
            self._state("speaking")
        return True

    def _try_play_intent(self, text: str):
        """'toque/toca/play <musica>' -> toca de verdade (YouTube autoplay); Spotify abre a busca."""
        t = (text or "").strip()
        m = re.search(r"(?i)\b(?:toc(?:a|ar|que)|play|bota pra tocar|p[oõ]e pra tocar|coloca pra tocar|"
                      r"quero ouvir|quero escutar|escuta(?:r)?)\s+(.+)", t)
        if not m:
            return None
        q = m.group(1).strip()
        wants_spotify = bool(re.search(r"(?i)spotify", t))
        q = re.sub(r"(?i)\s*\b(?:n[oa]|pelo|pela|via)\s+(spotify|youtube|yt|deezer).*$", "", q).strip()
        q = q.strip(" ?.!\"'")
        if len(q) < 2:
            return None
        if wants_spotify:
            try:
                subprocess.Popen(f'start spotify:search:{urllib.parse.quote(q)}', shell=True)
            except Exception:
                pass
        vid = youtube_first_video(q + " audio")
        if vid:
            try:
                subprocess.Popen(f'start "" "https://www.youtube.com/watch?v={vid}"', shell=True)
            except Exception:
                pass
            extra = " (e abri no Spotify pra você dar play lá também)" if wants_spotify else ""
            return f"🎵 Tocando '{q}' no YouTube{extra}!"
        try:
            subprocess.Popen(f'start "" "https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"', shell=True)
        except Exception:
            pass
        return f"Abri a busca de '{q}' no YouTube."

    def _app_command(self, text: str) -> bool:
        """Comandos de controle do app por voz/texto (print, silenciar, atualizar...)."""
        t = (text or "").strip().lower().rstrip("!.")
        if re.fullmatch(r"(?:para de falar|silenci\w*|cala a boca|fica quieta|shh+|quieta|cala)", t):
            self.stop_speak(); self._msg("sys", "🔇 Silenciei.", store=False); self._state("idle"); return True
        # Minecraft: entrar / sair
        if re.fullmatch(r"(?:entra(?:r)?|conecta(?:r)?|joga(?:r)?|vem|bora)\s+(?:n[oa]\s+)?minecraft|minecraft|modo minecraft", t):
            self.mc_start(); return True
        if re.fullmatch(r"(?:sai(?:r)?|desconecta(?:r)?|para(?:r)?)\s+(?:d[oe]\s+)?minecraft|sai do mine", t):
            self.mc_stop(); self._state("idle"); return True
        # Parar tudo (jogo / PC / showdown)
        if re.fullmatch(r"(?:parar?|para tudo|stop|chega|cancela(?:r)?)", t):
            self.stop_game(); self.stop_computer(); self.stop_showdown(); self._state("idle"); return True
        # Pokémon Showdown (batalha online)
        if re.fullmatch(r"(?:showdown|batalha(?:r)?(?: de| no)? pok\w*|joga(?:r)? showdown|pok\w* showdown)", t):
            self.showdown_play(); return True
        # Computer-use: "usa o pc pra ...", "controla o pc ...", "no navegador ..."
        mcu = re.match(r"(?i)^(?:usa(?:r)? o (?:pc|computador)(?: pra| para)?|controla(?:r)? o (?:pc|computador)|"
                       r"computer use|opera(?:r)? o (?:pc|navegador)|no navegador)\b(.*)", t)
        if mcu:
            self.computer_use(mcu.group(1).strip(" :,-")); return True
        # Modo jogo: "joga <jogo>", "zera <jogo>", "modo jogo", "para o jogo"
        if re.fullmatch(r"(?:para(?:r)?(?: o)?(?: modo)? jogo|stop game|para de jogar|sai do jogo)", t):
            self.stop_game(); self._state("idle"); return True
        mjogo = re.match(r"(?i)^(?:modo jogo|joga(?:r)?|zera(?:r)?|jogue|complete o jogo|passe? (?:de |o )?fase)\b(.*)", t)
        if mjogo:
            goal = mjogo.group(1).strip(" :,-") or ""
            self.play_game(goal); return True
        if re.fullmatch(r"(?:tira(?:r)? (?:um )?print|screenshot|captura(?:r)? a tela|print da tela|printa)", t):
            self.busy = True; self._state("thinking")
            threading.Thread(target=self._do_screenshot, daemon=True).start(); return True
        if re.fullmatch(r"(?:atualiz(?:a|ar)(?: a kemy)?|checa(?:r)? atualiza\w*|tem atualiza\w*)", t):
            self._msg("sys", "Procurando atualização…", store=False); self.check_update(); self._state("idle"); return True
        if re.fullmatch(r"(?:nova conversa|limpa(?:r)? (?:a )?conversa|comeca(?:r)? de novo)", t):
            self.new_convo(); self._state("idle"); return True
        if re.fullmatch(r"(?:abr(?:e|ir) o c[oó]digo|ver (?:o )?c[oó]digo|mostra(?:r)? o c[oó]digo)", t):
            self.browse_project(); self._state("idle"); return True
        return False

    def _do_screenshot(self) -> None:
        try:
            from PIL import ImageGrab
            # Sem window.minimize()/restore() (operacao de janela em thread trava o app).
            time.sleep(0.2)
            img = ImageGrab.grab()
            folder = Path.home() / "Pictures"
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"kemy_print_{int(time.time())}.png"
            img.save(dest)
            self.busy = False
            self._msg("kemy", f"📸 Print salvo em: {dest}")
            try:
                if os.name == "nt":
                    os.startfile(str(dest))  # type: ignore[attr-defined]
            except Exception:
                pass
            self._after_speak()
        except Exception as exc:
            self.busy = False
            self._msg("kemy", f"Não consegui tirar o print: {exc}")
            self._after_speak()

    def _chat_streaming(self, system: str, msgs: list) -> str:
        """Stream da resposta de conversa para a UI (texto em tempo real)."""
        self._js("startStream()")
        buf = {"t": "", "last": 0.0}

        def on_chunk(d: str) -> None:
            buf["t"] += d
            now = time.time()
            if now - buf["last"] >= 0.05:
                try:
                    self._js(f"streamChunk({json.dumps(buf['t'])})")
                except Exception:
                    pass
                buf["t"] = ""
                buf["last"] = now

        try:
            full = self.llm.chat_stream(system, msgs, on_chunk, max_tokens=700, fast=True)
        finally:
            if buf["t"]:
                try:
                    self._js(f"streamChunk({json.dumps(buf['t'])})")
                except Exception:
                    pass
            try:
                self._js("endStream()")
            except Exception:
                pass
        return full

    def _auto_learn(self, text: str) -> None:
        """Aprende sozinha: salva preferencias/correcoes na memoria, sem precisar dizer 'lembre'."""
        t = (text or "").strip()
        if len(t) > 160:
            return
        pats = [
            r"(?i)\b(?:eu )?prefiro\s+(.+)", r"(?i)\b(?:eu )?(?:nao|n[aã]o) gosto de\s+(.+)",
            r"(?i)\bodeio\s+(.+)", r"(?i)\bevite[a-z]*\s+(.+)", r"(?i)\b(?:nunca|jamais) (?:use|usar|faca|faça)\s+(.+)",
            r"(?i)\bsempre (?:use|usar|faca|faça)\s+(.+)", r"(?i)\b(?:nao|n[aã]o) (?:use|usar|faca|faça|coloque)\s+(.+)",
            r"(?i)\bnao e assim\b.*?[:,]?\s*(.+)", r"(?i)\b(?:ta|tá|esta|está) errad[oa]\b[,:]?\s*(.+)",
        ]
        for p in pats:
            m = re.search(p, t)
            if m:
                lesson = m.group(1).strip().rstrip(".!?").strip()
                if 2 < len(lesson) < 120 and lesson not in self.memories:
                    # reconstrói a licao com o verbo
                    full = t if t.lower().startswith(("prefiro", "nao", "não", "nunca", "sempre", "evite", "odeio")) else lesson
                    self.memories.append(full[:140])
                    save_memorias(self.memories)
                    self._msg("sys", "🧠 Anotei essa preferência pra próxima.", store=False)
                return

    def _memoria_prefix(self) -> str:
        if not self.memories:
            return ""
        return ("MEMORIA — licoes e preferencias que voce APRENDEU com este usuario "
                "(respeite SEMPRE, isso vale mais que regras gerais):\n- "
                + "\n- ".join(self.memories[-40:]) + "\n\n")

    def _knowledge_prefix(self, text: str) -> str:
        """Recupera (RAG) os fatos aprendidos mais relevantes para a pergunta."""
        if not self.knowledge:
            return ""
        words = set(re.findall(r"[\wáéíóúâêôãõç]{4,}", (text or "").lower()))
        if not words:
            return ""
        scored = []
        for k in self.knowledge:
            fw = set(re.findall(r"[\wáéíóúâêôãõç]{4,}", str(k.get("fato", "")).lower()))
            s = len(words & fw)
            if s:
                scored.append((s, k))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [k for _, k in scored[:6]]
        if not top:
            return ""
        linhas = "\n".join(f"- {k.get('fato','')} (fonte: {k.get('fonte','?')}, confianca: {k.get('confianca','?')}, {k.get('data','?')})"
                           for k in top)
        return ("CONHECIMENTO VERIFICADO (fatos que voce aprendeu e guardou — use se relevante, "
                "citando que tem essa info):\n" + linhas + "\n\n")

    def _maybe_learn_topic(self, text: str) -> bool:
        """Detecta 'aprenda sobre X / estude X / pesquise e guarde X' e dispara a ingestao."""
        m = re.match(r"(?i)^(?:aprend[ae]\s+(?:sobre\s+|mais sobre\s+)?|estud[ae]\s+(?:sobre\s+)?|"
                     r"pesquis[ae]\s+e\s+(?:guarde|salve|aprenda)\s+(?:sobre\s+)?|"
                     r"guard[ae]\s+(?:informacoes|informações|dados)\s+sobre\s+)(.+)$", text.strip())
        if not m:
            return False
        topic = m.group(1).strip().rstrip("?.!").strip()
        if len(topic) < 2:
            return False
        self._msg("user", text) if False else None
        self.busy = True
        self._state("thinking")
        threading.Thread(target=self._ingest_topic, args=(topic,), daemon=True).start()
        return True

    def _ingest_topic(self, topic: str) -> None:
        """Alfandega de dados: pesquisa, valida (fatos atomicos + confianca) e guarda."""
        try:
            self._msg("sys", f"📚 Pesquisando e validando sobre: {topic}…", store=False)
            results = web_search(topic, limit=4)
            links = re.findall(r"\((https?://[^)]+)\)", results)[:2]
            blocos = []
            for u in links:
                t = fetch_url_text(u, 5000)
                if t:
                    blocos.append(f"FONTE: {u}\n{t}")
            if not blocos:
                blocos = [f"RESULTADOS DA BUSCA:\n{results}"]
            raw = "\n\n".join(blocos)[:14000]
            alf_sys = (
                "Voce e uma ALFANDEGA DE DADOS rigorosa. Do texto bruto da web, extraia apenas FATOS ATOMICOS, "
                "verificaveis e uteis sobre o tema. REGRAS: cada fato curto e direto; ignore saudacoes, opinioes, "
                "anuncios e enrolacao; so inclua o que parece consistente/confiavel; de uma confianca a cada fato: "
                "'alta' (doc oficial, wikipedia, fonte institucional), 'media', 'baixa' (forum/blog anonimo). "
                "Responda APENAS um JSON array valido, no maximo 8 itens: "
                "[{\"fato\":\"...\",\"confianca\":\"alta|media|baixa\"}]. Sem texto fora do JSON.")
            out = self.llm.chat(alf_sys, [{"role": "user", "content": f"TEMA: {topic}\n\n{raw}"}],
                                 max_tokens=1500, fast=True)
            facts = self._parse_facts(out)
            hoje = datetime.date.today().isoformat()
            fonte = links[0] if links else "busca web"
            novos = 0
            existentes = {str(k.get("fato", "")).lower() for k in self.knowledge}
            for f in facts:
                fato = str(f.get("fato", "")).strip()
                if len(fato) < 5 or fato.lower() in existentes:
                    continue
                self.knowledge.append({"fato": fato, "confianca": f.get("confianca", "media"),
                                       "fonte": fonte, "data": hoje, "tema": topic})
                existentes.add(fato.lower())
                novos += 1
            if novos:
                save_conhecimento(self.knowledge)
            self.busy = False
            if novos:
                amostra = "\n".join(f"• {k['fato']}" for k in self.knowledge[-min(novos, 5):])
                self._msg("kemy", f"Aprendi e guardei {novos} fato(s) verificado(s) sobre **{topic}** 🧠📚:\n{amostra}")
                if self.speaker.available:
                    self.speaker.say(f"Aprendi {novos} coisas novas sobre {topic}.")
                    self._state("speaking")
                    return
            else:
                self._msg("kemy", f"Pesquisei sobre {topic}, mas não achei fatos novos e confiáveis pra guardar.")
            self._after_speak()
        except Exception as exc:
            self.busy = False
            self._msg("kemy", f"Não consegui estudar isso agora: {exc}")
            self._after_speak()

    def _parse_facts(self, out: str) -> list:
        try:
            mt = re.search(r"\[.*\]", out, re.DOTALL)
            data = json.loads(mt.group(0) if mt else out)
            return data if isinstance(data, list) else []
        except Exception:
            # fallback: linhas com "- fato"
            facts = []
            for ln in (out or "").splitlines():
                ln = ln.strip().lstrip("-•* ").strip()
                if len(ln) > 8:
                    facts.append({"fato": ln, "confianca": "media"})
            return facts[:8]

    def _process(self, text: str) -> None:
        try:
            if self.mode == "direct":
                chat, save = self._process_direct(text)
            else:
                chat, save = self._process_online(text)
        except Exception as exc:
            chat, save = (f"Falhei: {exc}", None)
        self.busy = False
        if getattr(self, "_streamed_done", False):
            # ja foi exibido em tempo real (streaming): so guarda no historico
            self._streamed_done = False
            it = self._cur()
            if it is not None and chat:
                it.setdefault("log", []).append({"r": "kemy", "t": chat})
                it["log"] = it["log"][-300:]
                self._save_convos()
        else:
            self._msg("kemy", chat or "Feito.", save)
        if self.speaker.available and chat:
            self.speaker.say(chat[:600])
            self._state("speaking")
        else:
            self._after_speak()

    def _process_direct(self, text: str):
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        # Acao direta: abrir site/app de verdade (sem depender da IA inventar).
        opened = self._try_open_intent(text)
        if opened is not None:
            return opened, None
        # Ver a tela do usuario.
        if re.search(r"(?i)(v[eê]j?a?|olh[ae]|enxerg\w+|analis\w+|print).{0,20}(minha )?tela|o que (tem|h[aá]|aparece|esta|tô|to|estou) (na|vendo na|aqui na)?\s*(minha )?tela", text):
            return self._screen_reply(text), None
        current = relevant_project_files(base, text)   # RAG de codebase (so o relevante em projetos grandes)
        # Conversa simples -> prompt LEVE e resposta rapida; criar/editar codigo -> prompt completo.
        build = is_build_request(text) or bool(current)
        msgs = []
        for e in (it.get("log") if it else []) or []:
            msgs.append({"role": "assistant" if e.get("r") == "kemy" else "user", "content": e.get("t", "")})
        web = self._web_context(text)
        self._auto_learn(text)            # aprende sozinha com preferencias/correcoes
        # 🤖 Modo agente autonomo (multi-passo) para tarefas que pedem "ate funcionar/completo".
        if build and self._wants_agent(text):
            return self._autonomous_agent(text, base), None
        mem = self._memoria_prefix() + self._knowledge_prefix(text)   # memoria + RAG de conhecimento
        if not build:
            system = mem + CHAT_PROMPT + (web or "")
            try:
                reply = self._chat_streaming(system, msgs[-8:])   # resposta em tempo real
                self._streamed_done = True
            except Exception:
                reply = self.llm.chat(system, msgs[-8:], max_tokens=700, fast=True)
            self._maybe_run(extract_run_commands(reply), base)  # caso ela mande abrir algo
            _, chat = parse_llm_files(reply)
            return (chat or reply).strip() or "…", None
        system = mem + SYSTEM_PROMPT
        if current:
            system += "\n\nARQUIVOS ATUAIS DO PROJETO (edite estes, nao recomece):\n" + current
        if web:
            system += web
        hist = msgs[-10:]
        complexo = len(text) > 70 or any(k in text.lower() for k in (
            "app", "sistema", "erp", "jogo", "game", "dashboard", "completo", "crud",
            "plataforma", "modulo", "módulo", "apresenta", "varios", "vários"))
        design_req = any(k in text.lower() for k in (
            "site", "página", "pagina", "landing", "app", "dashboard", "ui", "interface",
            "design", "portfolio", "portfólio", "loja", "ecommerce", "blog", "jogo", "game"))
        # 🎨 Modo Design dedicado: pedido claramente de UI/visual ganha o design-system premium
        # e SEMPRE usa o melhor modelo disponivel (NVIDIA frontier / GPT-5).
        if design_req:
            system += DESIGN_PROMPT
        # Painel "ver ela trabalhar" (checklist ao vivo). Substitui o spam de status no chat.
        panel_on = self.boost
        if panel_on:
            self._panel(["Analisar a tarefa", "Planejar a solução", "Gerar com o especialista",
                         "Revisar (olhar de sênior)", "Salvar e montar", "Rodar e mostrar"])
            self._panel_step(0, "doing")
        # Capricho (tecnicas nivel-pro): tudo silencioso, refletido no painel.
        if self.boost:
            if design_req:                    # pesquisa referencias antes (como um pro)
                refs = self._research_references(text)
                if refs:
                    system += "\n\nREFERENCIAS / INSPIRACAO (use as melhores ideias):\n" + refs
            self._panel_step(1, "doing")
            plano = self._plan(text)          # planejamento
            if plano:
                system += "\n\nPLANO A SEGUIR:\n" + plano
        # Roteador inteligente: escolhe a melhor IA pra tarefa (silencioso).
        route = self._smart_route(text)
        prefer = route[0] if route else ""
        if panel_on:
            self._panel_step(0, "done"); self._panel_step(1, "done"); self._panel_step(2, "doing")
        if self.boost and complexo and len(self.llm.providers()) >= 2:
            reply = self._moa(system, hist, text, route)  # Mixture of Agents (especialistas)
        else:
            reply = self.llm.chat(system, hist, max_tokens=16000, prefer=prefer)
        if panel_on:
            self._panel_step(2, "done")
        if self.boost:
            self._panel_step(3, "doing")
            reply = self._refine(system, hist, text, reply)   # revisao cruzada
            self._panel_step(3, "done")
        if panel_on:
            self._panel_step(4, "doing")
        files, chat = parse_llm_files(reply)
        edits0 = parse_edits(reply)
        self._apply_edits(edits0, base)  # edicoes cirurgicas (search/replace)
        save = self._save(files, base)
        if self.boost:
            self._run_and_fix(base, text)                # 4) Roda-e-corrige (Python)
        self._localize_images(base)
        self._ensure_scripts_linked(base)                # garante que app.js/css carreguem no index
        self._maybe_run(extract_run_commands(reply), base)
        self._gen_images(extract_image_requests(reply), base)
        self._gen_thumbs(extract_thumb_requests(reply), base)
        self._gen_graphics(extract_graphic_requests(reply), base)
        self._maybe_make_pdf(base, text, files)
        self._maybe_tests(base, text)                    # 5) Testes automaticos
        if files or edits0:
            self._git_snapshot(base, "kemy: " + text[:60])   # 6) Git: foto pra desfazer
        if panel_on:
            self._panel_step(4, "done"); self._panel_step(5, "doing")
        self._open_preview(base)                         # abre o preview SEMPRE no fim
        if panel_on:
            self._panel_step(5, "done"); self._panel_done()
        return chat or "Feito.", save

    def _refine(self, system: str, msgs: list, user_text: str, draft: str) -> str:
        """Autorrevisao: a Kemy critica seu rascunho (como um sr. engenheiro) e reescreve
        a versao final corrigida. Tecnica self-refine -> qualidade nivel pro, so com IA gratis."""
        self._state("thinking")
        review_sys = (system + "\n\n=== MODO REVISAO ===\nVoce vai REVISAR criticamente o rascunho que "
                      "voce mesma fez, como um engenheiro SENIOR exigente. Cheque: tem bug ou erro? esta "
                      "INCOMPLETO ou com placeholder? cada funcao/botao FUNCIONA de verdade? o design esta "
                      "bonito e profissional? o codigo esta limpo e organizado? falta tratar algum caso? "
                      "Corrija TODOS os problemas e entregue a VERSAO FINAL impecavel e COMPLETA, no mesmo "
                      "formato (<<<FILE>>> / <<<EDIT>>> / blocos). Entregue so a versao final, sem falar da revisao.")
        rmsgs = list(msgs) + [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": draft[:14000]},
            {"role": "user", "content": "Revise com olhar critico de senior e reentregue a VERSAO FINAL, "
             "completa, funcional e bonita. Se ja estiver perfeita, devolva igual."},
        ]
        provs = self.llm.providers()
        prefer = provs[1] if len(provs) > 1 else ""   # outro modelo = olhar fresco (revisao cruzada)
        try:
            improved = self.llm.chat(review_sys, rmsgs, max_tokens=16000, prefer=prefer)
            if improved and (FILE_RE.search(improved) or EDIT_RE.search(improved) or len(improved) > 200):
                return improved
        except Exception:
            pass
        return draft

    def _plan(self, text: str) -> str:
        """Planejamento (chain-of-thought): plano objetivo antes de codar."""
        try:
            psys = ("Voce e um engenheiro senior. Em ate 8 linhas, faca um PLANO objetivo para atender o "
                    "pedido: quais arquivos criar/editar, a abordagem/biblioteca e os passos. So o plano, sem codigo.")
            return self.llm.chat(psys, [{"role": "user", "content": text}], max_tokens=500, fast=True)
        except Exception:
            return ""

    def _route(self, text: str) -> list:
        """Roteia a tarefa pra MELHOR IA: classifica o pedido e ordena os provedores por
        especialidade (cada IA boa no que faz). Retorna a lista de provedores em ordem."""
        t = (text or "").lower()
        provs = self.llm.providers()

        def order(pref):
            out = [p for p in pref if p in provs]
            out += [p for p in provs if p not in out]
            return out

        code = ("erp", "sistema", "backend", "api", "servidor", "django", "flask", "fastapi",
                "node", "sql", "banco", "codigo", "código", "funcao", "função", "bug", "corrig",
                "script", "app", "crud", "classe", "refator")
        design = ("site", "landing", "design", "ui", "interface", "pagina", "página", "portfolio",
                  "portfólio", "loja", "tema", "layout", "thumb", "poster", "banner", "css", "visual")
        writing = ("explica", "resuma", "resumo", "escreve", "escreva", "texto", "redaç", "artigo",
                   "ideia", "planeje", "plano", "estrateg", "estratég", "analise", "análise", "traduz")
        if any(k in t for k in code):
            # Kimi K2 (Groq) primeiro pra codigo; depois Cerebras Qwen-Coder e NVIDIA DeepSeek.
            return order(["groq", "cerebras", "nvidia", "mistral", "sambanova", "github"])
        if any(k in t for k in design):
            # Design/UI: GPT-5 (GitHub) e GLM-5.1/DeepSeek (NVIDIA) sao os melhores gratis.
            return order(["github", "nvidia", "gemini", "cerebras", "groq"])
        if any(k in t for k in writing):
            return order(["gemini", "github", "nvidia", "groq", "cerebras"])
        return order(["nvidia", "cerebras", "groq", "gemini"])

    def _smart_route(self, text: str) -> list:
        """A Kemy ANALISA a tarefa (uma IA rapida classifica) e ESCOLHE o melhor especialista.
        Cai pro roteador por palavra-chave se a classificacao falhar."""
        base = self._route(text)
        self._last_cat = ""
        if len((text or "").strip()) < 12 or len(self.llm.providers()) < 2:
            return base
        try:
            cls = self.llm.chat(
                "Classifique a tarefa do usuario em UMA categoria, respondendo SO a palavra: "
                "code (programar/sistema/bug), design (site/ui/visual), writing (texto/explicar/ideia), "
                "reasoning (planejar/analisar/decidir), data (dados/planilha/banco), chat (conversa).",
                [{"role": "user", "content": (text or "")[:600]}], max_tokens=6, fast=True)
            cat = next((c for c in ("code", "design", "writing", "reasoning", "data", "chat")
                        if c in (cls or "").lower()), "")
        except Exception:
            return base
        mapping = {
            "code": ["groq", "cerebras", "nvidia", "mistral", "sambanova", "github"],
            "design": ["github", "nvidia", "gemini", "cerebras", "groq"],
            "writing": ["gemini", "github", "nvidia", "groq", "cerebras"],
            "reasoning": ["nvidia", "github", "groq", "gemini", "cerebras"],
            "data": ["nvidia", "cerebras", "groq", "gemini"],
            "chat": ["gemini", "cerebras", "groq"],
        }
        if cat not in mapping:
            return base
        self._last_cat = cat
        provs = self.llm.providers()
        return [p for p in mapping[cat] if p in provs] + [p for p in provs if p not in mapping[cat]]

    def _spec_label(self, text: str, prov: str) -> str:
        if prov == "groq":
            return "Kimi K2 (código)"
        if prov == "github":
            return "GPT-5 (design)"
        names = {"nvidia": "NVIDIA (DeepSeek-V4 / GLM-5.1)", "cerebras": "Cerebras (Qwen-Coder 480B)",
                 "gemini": "Gemini 3", "mistral": "Mistral (Codestral)",
                 "sambanova": "SambaNova", "openai": "OpenAI", "openrouter": "OpenRouter"}
        return names.get(prov, prov)

    def _moa(self, system: str, msgs: list, text: str, route: list | None = None) -> str:
        """Mixture of Agents: 2 ESPECIALISTAS (modelos diferentes, escolhidos pela tarefa)
        geram, e um modelo forte junta o melhor dos dois."""
        provs = route or self.llm.providers()
        drafts = []
        for prov in provs[:2]:
            try:
                d = self.llm.chat(system, msgs, max_tokens=16000, prefer=prov)
                if d:
                    drafts.append(d)
            except Exception:
                pass
        if len(drafts) < 2:
            return drafts[0] if drafts else self.llm.chat(system, msgs, max_tokens=16000)
        synth_sys = (system + "\n\nVoce recebeu DUAS solucoes de modelos diferentes para o mesmo pedido. "
                     "Combine o MELHOR de cada uma numa unica VERSAO FINAL superior — mais completa, correta e "
                     "bonita — no mesmo formato. Nao comente, so entregue a versao final.")
        smsgs = list(msgs) + [
            {"role": "user", "content": text},
            {"role": "assistant", "content": "SOLUCAO A:\n" + drafts[0][:9000]},
            {"role": "assistant", "content": "SOLUCAO B:\n" + drafts[1][:9000]},
            {"role": "user", "content": "Junte o melhor das duas e entregue a versao final unica e impecavel."},
        ]
        try:
            return self.llm.chat(synth_sys, smsgs, max_tokens=16000)
        except Exception:
            return drafts[0]

    def _run_and_fix(self, base: Path, text: str, max_iters: int = 2) -> None:
        """Loop agentico: roda o codigo Python, le o erro e conserta sozinha (estilo Codex)."""
        pys = list(base.glob("*.py"))
        if not pys:
            return
        main = next((p for p in pys if p.name in ("main.py", "app.py", "run.py")), pys[0])
        for i in range(max_iters):
            err = None
            for pyexe in ("python", "py", "python3"):
                try:
                    proc = subprocess.run([pyexe, str(main)], cwd=str(base),
                                          capture_output=True, text=True, timeout=20)
                    err = (proc.stderr or "").strip()
                    if proc.returncode == 0 or "Traceback" not in err:
                        if i > 0:
                            self._msg("sys", "✅ Testei e corrigi: roda sem erro agora.", store=False)
                        return
                    break
                except FileNotFoundError:
                    continue
                except Exception:
                    return
            if err is None:   # sem Python instalado
                return
            self._msg("sys", f"🐞 Testei o código e deu erro — corrigindo sozinha (tentativa {i+1})…", store=False)
            ctx = read_project_files(base)
            fsys = SYSTEM_PROMPT + "\n\nO CODIGO DEU ERRO AO RODAR. Conserte de forma cirurgica com <<<EDIT>>>."
            fmsgs = [{"role": "user", "content": f"Pedido: {text}\n\nARQUIVOS:\n{ctx}\n\nERRO ao rodar "
                      f"{main.name}:\n{err[-1800:]}\n\nConserte o bug e entregue so as edicoes."}]
            try:
                r = self.llm.chat(fsys, fmsgs, max_tokens=12000)
            except Exception:
                return
            self._apply_edits(parse_edits(r), base)
            self._save(parse_llm_files(r)[0], base)

    # ---- Git (versionamento / desfazer) ----
    def _git(self, base: Path, args: list, timeout: int = 25):
        try:
            p = subprocess.run(["git"] + args, cwd=str(base), capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception:
            return 1, ""

    def _git_snapshot(self, base: Path, msg: str) -> None:
        """Salva uma 'foto' (commit) do projeto para dar pra desfazer depois."""
        if not base.exists():
            return
        rc, _ = self._git(base, ["rev-parse", "--is-inside-work-tree"])
        if rc != 0:
            if self._git(base, ["init"])[0] != 0:
                return  # git nao instalado
            self._git(base, ["config", "user.email", "kemy@local"])
            self._git(base, ["config", "user.name", "Kemy"])
        self._git(base, ["add", "-A"])
        self._git(base, ["commit", "-m", (msg[:80] or "kemy: alteracao")])

    def git_undo(self) -> None:
        """Desfaz a última alteração (volta ao commit anterior)."""
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        rc, _ = self._git(base, ["rev-parse", "--is-inside-work-tree"])
        if rc != 0:
            self._msg("sys", "Não há histórico (Git) pra desfazer neste projeto ainda.", store=False)
            return
        _, cnt = self._git(base, ["rev-list", "--count", "HEAD"])
        try:
            n = int(cnt.strip())
        except Exception:
            n = 0
        if n >= 2:
            self._git(base, ["reset", "--hard", "HEAD~1"])
            self._msg("sys", "↩️ Desfeito! Voltei o projeto pra versão anterior.", store=False)
        elif n == 1:
            self._git(base, ["reset", "--hard", "HEAD"])
            self._msg("sys", "↩️ Descartei as alterações não salvas.", store=False)
        else:
            self._msg("sys", "Sem versão anterior pra voltar.", store=False)
        idx = base / "index.html"
        if idx.exists():
            try:
                webbrowser.open(idx.as_uri())
            except Exception:
                pass

    def _maybe_tests(self, base: Path, text: str) -> None:
        """Gera e roda testes (pytest/unittest) quando o usuario pede, e tenta corrigir falhas."""
        if not any(k in text.lower() for k in ("teste", "testes", "unit test", "pytest", "testar o codigo", "testar o código")):
            return
        pys = [p for p in base.glob("*.py") if not p.name.lower().startswith("test")]
        if not pys:
            return
        self._msg("sys", "🧪 Escrevendo e rodando testes…", store=False)
        ctx = read_project_files(base)
        tsys = (SYSTEM_PROMPT + "\n\nEscreva TESTES automatizados (pytest, ou unittest da stdlib se pytest nao "
                "existir) cobrindo as funcoes principais. Entregue o arquivo test_app.py completo no formato "
                "<<<FILE: test_app.py>>>...<<<END>>>. So o arquivo de teste.")
        try:
            r = self.llm.chat(tsys, [{"role": "user", "content": f"Pedido: {text}\n\nCODIGO:\n{ctx}\n\n"
                                      "Escreva testes que cobrem o comportamento principal."}], max_tokens=8000)
            self._save(parse_llm_files(r)[0], base)
        except Exception:
            return
        out = self._run_tests(base)
        if out is None:
            self._msg("sys", "🧪 Testes criados. (Instale o Python/pytest pra eu rodá-los — com Auto eu instalo.)", store=False)
            return
        passed = ("passed" in out.lower() or "ok" in out.lower()) and "fail" not in out.lower() and "error" not in out.lower()
        self._msg("sys", f"🧪 Testes:\n{out[:600]}", store=False)
        if not passed and self.boost:
            self._msg("sys", "🐞 Teste falhou — corrigindo…", store=False)
            ctx2 = read_project_files(base)
            fsys = SYSTEM_PROMPT + "\n\nOs TESTES FALHARAM. Conserte o codigo (nao os testes) com <<<EDIT>>>."
            try:
                r2 = self.llm.chat(fsys, [{"role": "user", "content": f"ARQUIVOS:\n{ctx2}\n\nSAIDA DOS TESTES:\n{out[-1500:]}\n\nConserte."}], max_tokens=10000)
                self._apply_edits(parse_edits(r2), base)
                self._save(parse_llm_files(r2)[0], base)
                out2 = self._run_tests(base)
                if out2:
                    self._msg("sys", f"🧪 Após correção:\n{out2[:500]}", store=False)
            except Exception:
                pass

    def _run_tests(self, base: Path):
        for cmd in (["python", "-m", "pytest", "-q"], ["py", "-m", "pytest", "-q"], ["python3", "-m", "pytest", "-q"]):
            try:
                p = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=60)
                o = ((p.stdout or "") + (p.stderr or "")).strip()
                if "No module named pytest" in o:
                    # tenta unittest
                    break
                return o or "(sem saída)"
            except FileNotFoundError:
                continue
            except Exception:
                return None
        for cmd in (["python", "-m", "unittest", "discover", "-q"], ["py", "-m", "unittest", "discover", "-q"]):
            try:
                p = subprocess.run(cmd, cwd=str(base), capture_output=True, text=True, timeout=60)
                return ((p.stdout or "") + (p.stderr or "")).strip() or "(sem saída)"
            except FileNotFoundError:
                continue
            except Exception:
                return None
        return None

    def _wants_agent(self, text: str) -> bool:
        t = (text or "").lower()
        # gatilhos explicitos
        if any(k in t for k in (
            "modo agente", "agente", "passo a passo", "ate funcionar", "até funcionar",
            "rode e ", "monte e ", "crie e teste", "projeto completo", "faca funcionar",
            "faça funcionar", "complete o projeto", "termine o projeto", "ate concluir", "até concluir")):
            return True
        # objetivos GRANDES (sistema/ERP/plataforma) entregam melhor no agente autonomo
        big = any(k in t for k in ("erp", "sistema", "plataforma", "dashboard completo", "painel admin",
                                   "saas", "marketplace", "aplicativo completo", "app completo"))
        return big and len(t) > 25

    def _run_capture(self, cmds: list, base: Path) -> str:
        outs = []
        for cmd in cmds[:6]:
            self._msg("sys", f"$ {cmd}", store=False)
            run = cmd
            if run.strip().lower().startswith(("http://", "https://")):
                run = f'start "" "{run.strip()}"'
            try:
                p = subprocess.run(run, shell=True, cwd=str(base), capture_output=True, text=True, timeout=120)
                o = ((p.stdout or "") + (p.stderr or "")).strip()
                if o:
                    self._msg("sys", o[:600], store=False)
                outs.append(f"$ {cmd}\n{o[:1500]}")
            except subprocess.TimeoutExpired:
                outs.append(f"$ {cmd}\n(demorou demais / timeout)")
            except Exception as e:
                outs.append(f"$ {cmd}\n(erro: {e})")
        return "\n".join(outs)

    def _run_py_capture(self, base: Path):
        pys = list(base.glob("*.py"))
        if not pys:
            return None
        main = next((p for p in pys if p.name in ("main.py", "app.py", "run.py")), pys[0])
        for pyexe in ("python", "py", "python3"):
            try:
                p = subprocess.run([pyexe, str(main)], cwd=str(base), capture_output=True, text=True, timeout=20)
                out = ((p.stdout or "") + (p.stderr or "")).strip()
                self._msg("sys", f"$ python {main.name}\n{(out[:500] or '(sem saída)')}", store=False)
                return f"python {main.name} (retcode {p.returncode}):\n{out[:1500]}"
            except FileNotFoundError:
                continue
            except Exception:
                return None
        return None

    def _agent_loop(self, text: str, base: Path, max_steps: int = 5) -> str:
        """Agente autonomo (estilo Codex/Claude Code): trabalha em passos — cria, roda, le a
        saida/erro e corrige — ate concluir a tarefa."""
        base.mkdir(parents=True, exist_ok=True)
        self._msg("sys", "🤖 Modo agente ligado — vou trabalhar em passos até terminar.", store=False)
        last_output = ""
        for step in range(1, max_steps + 1):
            self._state("thinking")
            files_ctx = read_project_files(base)
            sys_p = (self._memoria_prefix() + SYSTEM_PROMPT + "\n\n=== MODO AGENTE ===\nVoce trabalha em PASSOS "
                     "ate CONCLUIR a tarefa, como um engenheiro autonomo. A cada passo: crie/edite arquivos "
                     "(<<<FILE>>>/<<<EDIT>>>) e, se precisar instalar/rodar/testar, use ```kemy-run (voce VE a "
                     "saida e os erros e continua corrigindo). Avance de verdade a cada passo, nao repita o que ja "
                     "esta feito. Quando estiver 100% pronto E funcionando, escreva <<<DONE>>> numa linha e um "
                     "resumo curto do que entregou.")
            usr = (f"TAREFA: {text}\n\nARQUIVOS ATUAIS DO PROJETO:\n{files_ctx or '(vazio)'}\n\n"
                   f"SAIDA DO ULTIMO COMANDO/EXECUCAO:\n{last_output[-1600:] or '(nada ainda)'}\n\n"
                   f"Continue (passo {step}). Se ja concluiu, responda com <<<DONE>>> e o resumo.")
            try:
                reply = self.llm.chat(sys_p, [{"role": "user", "content": usr}], max_tokens=16000)
            except Exception as exc:
                return f"O agente parou por um erro: {exc}"
            self._msg("sys", f"🤖 Passo {step}/{max_steps}…", store=False)
            files, chat = parse_llm_files(reply)
            edits = parse_edits(reply)
            self._apply_edits(edits, base)
            self._save(files, base)
            self._localize_images(base)
            self._ensure_scripts_linked(base)
            cmds = extract_run_commands(reply)
            if cmds:
                last_output = self._run_capture(cmds, base)
            else:
                pyout = self._run_py_capture(base)
                last_output = pyout if pyout is not None else ""
            if "<<<DONE>>>" in reply:
                summ = reply.split("<<<DONE>>>")[-1].strip() or chat or "Tarefa concluída!"
                self._msg("sys", f"✅ Agente concluiu em {step} passo(s).", store=False)
                self._open_preview(base)
                return summ[:700] or "Pronto, tarefa concluída!"
            if not files and not edits and not cmds and step >= 2:
                self._open_preview(base)
                return (chat or "Acho que terminei — dá uma olhada e me diz se falta algo.")[:700]
        self._open_preview(base)
        return "Cheguei no limite de passos. O projeto avançou bastante — me diz o que ainda falta que eu continuo."

    def _agent_plan(self, text: str) -> list:
        """Quebra o objetivo em 3-7 tarefas concretas e ordenadas (pro painel ao vivo)."""
        try:
            out = self.llm.chat(
                "Voce e um engenheiro senior. Quebre o OBJETIVO do usuario em 3 a 7 TAREFAS concretas, "
                "na ordem certa pra ENTREGAR funcionando (ex.: 'Criar modelos e banco', 'CRUD de vendas', "
                "'UI do painel', 'Rodar e corrigir'). Responda SO um JSON array de strings curtas.",
                [{"role": "user", "content": (text or "")[:800]}], max_tokens=400, fast=True)
            m = re.search(r"\[.*\]", out or "", re.DOTALL)
            arr = json.loads(m.group(0)) if m else []
            steps = [str(s).strip() for s in arr if str(s).strip()][:7]
            return steps
        except Exception:
            return []

    def _agent_do_task(self, objective: str, task: str, base: Path, plan: list, last_output: str):
        """Executa UMA tarefa do plano (gera/edita arquivos, roda comandos). Retorna (ok, saida, nota)."""
        route = self._smart_route(objective + " " + task)
        prefer = route[0] if route else ""
        files_ctx = relevant_project_files(base, task + " " + objective) or read_project_files(base)
        sysp = (self._memoria_prefix() + SYSTEM_PROMPT + "\n\n=== AGENTE: TAREFA ATUAL ===\n"
                "Faca SO a tarefa atual do plano, COMPLETA e funcional. Crie/edite arquivos "
                "(<<<FILE>>>/<<<EDIT>>>); se precisar instalar/rodar/testar, use ```kemy-run. NAO refaca o "
                "que ja existe. Lembre: todo botao/rota tem que funcionar e os dados persistem no banco.")
        usr = (f"OBJETIVO GERAL: {objective}\nPLANO: {plan}\nTAREFA ATUAL: {task}\n\n"
               f"ARQUIVOS ATUAIS:\n{files_ctx or '(vazio)'}\n\nSAIDA ANTERIOR:\n{last_output[-1200:] or '(nada)'}")
        try:
            reply = self.llm.chat(sysp, [{"role": "user", "content": usr}], max_tokens=16000, prefer=prefer)
        except Exception as e:
            return False, last_output, f"erro ({e})"
        files, chat = parse_llm_files(reply)
        edits = parse_edits(reply)
        if edits:
            self._apply_edits(edits, base)
        if files:
            self._save(files, base)
        self._sanitize_python(base)
        out = last_output
        cmds = extract_run_commands(reply)
        if cmds:
            out = self._run_capture(cmds, base)
        self._gen_images(extract_image_requests(reply), base)
        self._gen_graphics(extract_graphic_requests(reply), base)
        return True, out, (chat or task)[:90]

    def _autonomous_agent(self, text: str, base: Path) -> str:
        """Agente autônomo (objetivo → entrega) com painel ao vivo: planeja em tarefas, executa
        uma a uma (gera, roda, corrige) e entrega o resultado pronto/rodando — estilo Manus."""
        base.mkdir(parents=True, exist_ok=True)
        self._msg("kemy", "🤖 Modo agente ligado! Vou planejar isso e entregar pronto. Acompanha no painel 👇")
        plan = self._agent_plan(text) or ["Montar o projeto", "Implementar as funcionalidades",
                                          "Rodar e corrigir", "Entregar funcionando"]
        # garante uma etapa final de verificacao/entrega
        if not any("rod" in s.lower() or "test" in s.lower() or "entreg" in s.lower() for s in plan):
            plan.append("Rodar e entregar funcionando")
        self._panel(plan)
        last_output, notes = "", []
        for i, task in enumerate(plan):
            self._panel_step(i, "doing")
            self._msg("sys", f"🤖 {i + 1}/{len(plan)}: {task}", store=False)
            ok, last_output, note = self._agent_do_task(text, task, base, plan, last_output)
            self._panel_step(i, "done" if ok else "fail")
            if note:
                notes.append(f"• {note}")
        # Entrega: garante deps/scripts e SOBE o servidor / abre o preview (com auto-fix).
        self._sanitize_python(base)
        self._localize_images(base)
        self._ensure_scripts_linked(base)
        self._maybe_make_pdf(base, text, [])
        try:
            self._git_snapshot(base, "kemy agente: " + text[:50])
        except Exception:
            pass
        self._panel_done()
        self._open_preview(base)
        resumo = "\n".join(notes[:6])
        return f"✅ Entreguei! Trabalhei em {len(plan)} etapas:\n{resumo}\n\nDá uma olhada — me diz se quer ajustar algo."

    def _maybe_make_pdf(self, base: Path, text: str, files: list) -> None:
        """Se o usuario pediu PDF, converte o HTML gerado (documento/relatorio) em PDF."""
        if "pdf" not in (text or "").lower():
            return
        try:
            htmls = sorted(base.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            htmls = []
        if not htmls:
            return
        # prefere um html que nao seja 'index' (doc/relatorio), senao usa o mais recente
        src = next((h for h in htmls if h.stem.lower() != "index"), htmls[0])
        pdf = src.with_suffix(".pdf")
        self._msg("sys", "📄 Gerando o PDF…", store=False)
        if html_to_pdf(src, pdf):
            self._msg("sys", f"✅ PDF pronto: {pdf.name} (em {base})", store=False)
            try:
                if os.name == "nt":
                    os.startfile(str(pdf))  # type: ignore[attr-defined]
                else:
                    webbrowser.open(pdf.as_uri())
            except Exception:
                pass
        else:
            self._msg("sys", "Gerei o documento em HTML; pra virar PDF abra ele e use Ctrl+P → Salvar como PDF.", store=False)

    def _localize_images(self, base: Path) -> None:
        """Baixa as imagens do Pollinations citadas no HTML/CSS e troca por arquivos locais,
        para que o site nao dependa de fetch ao vivo (que vinha quebrado)."""
        targets = list(base.glob("*.html")) + list(base.glob("*.css")) + list(base.glob("*.js"))
        if not targets:
            return
        url_re = re.compile(r"https://image\.pollinations\.ai/prompt/[^\s\"')]+")
        # coleta todas as URLs unicas
        all_urls: list[str] = []
        contents: dict[Path, str] = {}
        for f in targets:
            try:
                txt = f.read_text(encoding="utf-8")
            except Exception:
                continue
            contents[f] = txt
            for u in url_re.findall(txt):
                if u not in all_urls:
                    all_urls.append(u)
        if not all_urls:
            return
        self._msg("sys", f"🖼 Gerando {len(all_urls)} imagem(ns) do site (Nano Banana, alta qualidade)…", store=False)
        assets = base / "assets"
        mapping: dict[str, str] = {}
        for i, u in enumerate(all_urls[:12], 1):
            # extrai o prompt e o tamanho da URL do Pollinations pra REGERAR no Nano Banana.
            prompt, size = "", "1024x1024"
            try:
                m = re.search(r"/prompt/([^?]+)", u)
                if m:
                    prompt = urllib.parse.unquote(m.group(1))
                qw = re.search(r"width=(\d+)", u); qh = re.search(r"height=(\d+)", u)
                if qw and qh:
                    size = f"{qw.group(1)}x{qh.group(1)}"
            except Exception:
                pass
            png = assets / f"img{i}.png"
            if prompt and GEMINI_IMAGE_KEY and download_image(prompt, png, size):
                mapping[u] = f"assets/img{i}.png"
            else:
                jpg = assets / f"img{i}.jpg"   # reserva: baixa a do Pollinations
                if download_to(u, jpg):
                    mapping[u] = f"assets/img{i}.jpg"
        if not mapping:
            return
        for f, txt in contents.items():
            new = txt
            for u, local in mapping.items():
                new = new.replace(u, local)
            if new != txt:
                try:
                    f.write_text(new, encoding="utf-8")
                except Exception:
                    pass
        self._msg("sys", f"✅ {len(mapping)} imagem(ns) salvas em /assets e aplicadas ao site.", store=False)

    def _web_context(self, text: str) -> str:
        """Le URLs citadas e faz busca na web quando o usuario pede; alimenta a IA."""
        extra = ""
        urls = re.findall(r"https?://[^\s)>\"']+", text)
        for u in urls[:2]:
            self._msg("sys", f"🌐 Lendo {u} …", store=False)
            t = fetch_url_text(u)
            if t:
                extra += f"\n\nCONTEUDO DE {u}:\n{t}"
        low = text.lower().strip()
        triggers = ("pesquise", "pesquisar", "busque", "buscar", "procure", "procurar", "search")
        if any(low.startswith(p) for p in triggers) or "na internet" in low or "na web" in low:
            q = text.split(":", 1)[1].strip() if ":" in text else text
            self._msg("sys", f"🔎 Buscando na web: {q}", store=False)
            r = web_search(q)
            if r:
                extra += f"\n\nRESULTADOS DA WEB para '{q}':\n{r}"
        if extra:
            return ("\n\nINFORMACOES DA WEB (use estes dados atuais para responder; "
                    "cite os links quando relevante):" + extra)
        return ""

    def _gen_images(self, reqs: list[dict], base: Path) -> None:
        if not reqs:
            return
        self._msg("sys", f"🎨 Gerando {len(reqs)} imagem(ns)…", store=False)
        ok: list[str] = []
        for r in reqs[:6]:
            dest = base / r["file"]
            if download_image(r["prompt"], dest, r.get("size", "1024x1024")):
                ok.append(r["file"])
        if ok:
            self._msg("sys", f"🖼 Pronto: {', '.join(ok)} (em {base})", store=False)
            try:
                first = base / ok[0]
                if os.name == "nt":
                    os.startfile(str(first))  # type: ignore[attr-defined]
                else:
                    webbrowser.open(first.as_uri())
            except Exception:
                pass
        else:
            self._msg("sys", "Nao consegui gerar a imagem agora (tente de novo).", store=False)

    def _thumb_reference(self, topic: str) -> str:
        """Olha thumbnails reais parecidas e extrai o estilo (como um designer faz)."""
        if not self.llm.gemini:
            return ""
        try:
            urls = web_image_search((topic or "").strip()[:80] + " youtube thumbnail", 3)
            for u in urls[:3]:
                try:
                    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                    data = urllib.request.urlopen(req, timeout=12).read()
                    if len(data) < 2000 or len(data) > 6_000_000:
                        continue
                    mime = "image/png" if u.lower().split("?")[0].endswith(".png") else "image/jpeg"
                    b64 = base64.b64encode(data).decode("ascii")
                    style = self.llm.vision(
                        "Esta e uma thumbnail de referencia. Descreva em INGLES, em UMA frase curta, o estilo "
                        "visual (cores dominantes, composicao, expressao/emocao, energia) para eu recriar algo "
                        "no mesmo estilo. So a frase, sem rodeios.", b64, mime)
                    if style and 5 < len(style) < 400:
                        return style.strip().replace("\n", " ")
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    def _research_references(self, text: str) -> str:
        """Pesquisa referencias/melhores praticas antes de construir (grounding)."""
        try:
            results = web_search(text[:80] + " design inspiration exemplos melhores praticas", 4)
            if not results:
                return ""
            links = re.findall(r"\((https?://[^)]+)\)", results)[:1]
            extra = fetch_url_text(links[0], 2500) if links else ""
            ins = self.llm.chat(
                "Resuma em ate 6 bullets curtos, em portugues, as melhores praticas e ideias de design/estrutura "
                "RELEVANTES para o pedido (cores, secoes, recursos, libs). Direto, sem enrolar.",
                [{"role": "user", "content": f"PEDIDO: {text}\n\nREFERENCIAS DA WEB:\n{(results + chr(10) + extra)[:6000]}"}],
                max_tokens=400, fast=True)
            return ins.strip() if ins else ""
        except Exception:
            return ""

    def _gen_thumbs(self, reqs: list[dict], base: Path) -> None:
        if not reqs:
            return
        self._msg("sys", "🎬 Montando 2 opções de thumbnail pra você escolher…", store=False)
        ok: list[str] = []
        for r in reqs[:2]:
            title = r.get("title", "")
            style = r.get("style", "anime")
            scene = r.get("scene", "")
            ref = self._thumb_reference(f"{title} {scene}")   # inspira em referencias reais
            if ref:
                self._msg("sys", "🔎 Me inspirei em thumbnails reais parecidas.", store=False)
                scene = (scene + ", visual style inspired by: " + ref)[:420]
            stem, ext = os.path.splitext(r["file"])
            for v in range(2):   # 2 variacoes (seeds/arte diferentes)
                dest = base / (r["file"] if v == 0 else f"{stem}_op{v+1}{ext}")
                if make_thumbnail(title, scene, dest, style):
                    ok.append(dest.name)
        if ok:
            self._msg("sys", f"🖼 {len(ok)} opções prontas: {', '.join(ok)} — escolha a que mais gostou! (em {base})", store=False)
            for name in ok[:4]:
                try:
                    p = base / name
                    if os.name == "nt":
                        os.startfile(str(p))  # type: ignore[attr-defined]
                    else:
                        webbrowser.open(p.as_uri())
                except Exception:
                    pass
        else:
            self._msg("sys", "Nao consegui montar a thumbnail agora (tente de novo).", store=False)

    def _gen_graphics(self, reqs: list[dict], base: Path) -> None:
        """Gera artes (post/story/banner/poster/thumb) com TEXTO NITIDO sobre arte Flux."""
        if not reqs:
            return
        self._msg("sys", f"🎨 Criando {min(len(reqs),6)} arte(s) com texto nítido…", store=False)
        ok: list[str] = []
        for r in reqs[:6]:
            title = r.get("title", "")
            subtitle = r.get("subtitle", "")
            scene = r.get("scene", "")
            style = r.get("style", "modern")
            fmt = r.get("fmt", "post")
            if make_graphic(title, subtitle, scene, base / r["file"], style, fmt):
                ok.append(r["file"])
        if ok:
            self._msg("sys", f"🖼 Pronto: {', '.join(ok)} (em {base})", store=False)
            for name in ok[:4]:
                try:
                    p = base / name
                    if os.name == "nt":
                        os.startfile(str(p))  # type: ignore[attr-defined]
                    else:
                        webbrowser.open(p.as_uri())
                except Exception:
                    pass
        else:
            self._msg("sys", "Nao consegui criar a arte agora (tente de novo).", store=False)

    def _process_online(self, text: str):
        it = self._cur()
        sid = it.get("session_id") if it else None
        if not sid:
            sid = self.api.new_session()
            if it is not None:
                it["session_id"] = sid
                self._save_convos()
        self.api.session_id = sid
        q = self.api.send_command(text, sid, modo="coding")
        job = q.get("job_id")
        resultado, erro = None, None
        for _ in range(600):
            j = self.api.job_status(job)
            if j.get("status") in ("done", "error", "canceled"):
                resultado = j.get("resultado")
                erro = j.get("erro") if j.get("status") == "error" else None
                break
            time.sleep(0.5)
        spoken, display = result_to_speech(resultado, erro)
        files = (resultado or {}).get("files") or extract_code_files(str((resultado or {}).get("raw") or ""))
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        return display, self._save(files, base)

    def _save(self, files: list[dict], base: Path) -> str | None:
        import difflib
        if not files:
            return None
        n, changed = 0, []
        for f in files:
            rel = str(f.get("path") or "").strip().lstrip("/\\")
            content = f.get("content")
            if not rel or content is None:
                continue
            dest = base / rel
            old = ""
            try:
                if dest.exists():
                    old = dest.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                old = ""
            new = str(content)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(new, encoding="utf-8", errors="ignore")
            except Exception:
                continue
            n += 1
            changed.append(self._register_change(rel, old, new))
        if not n:
            return None
        self._show_chips(changed)
        return None  # preview e aberto no fim do build; chips mostram os arquivos

    def _ensure_scripts_linked(self, base: Path) -> None:
        """Garante que o index.html carregue TODOS os .js e .css do projeto (evita 'app.js que nao abre')."""
        idx = base / "index.html"
        if not idx.exists():
            return
        try:
            html = idx.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        changed = False
        for css in sorted(base.glob("*.css")):
            nm = css.name
            if f'href="{nm}"' not in html and f"href='{nm}'" not in html:
                link = f'<link rel="stylesheet" href="{nm}">'
                html = html.replace("</head>", f"  {link}\n</head>", 1) if "</head>" in html else link + "\n" + html
                changed = True
        for js in sorted(base.glob("*.js")):
            nm = js.name
            if f'src="{nm}"' not in html and f"src='{nm}'" not in html:
                tag = f'<script src="{nm}"></script>'
                html = html.replace("</body>", f"  {tag}\n</body>", 1) if "</body>" in html else html + f"\n{tag}\n"
                changed = True
        if changed:
            try:
                idx.write_text(html, encoding="utf-8")
            except Exception:
                pass

    def _python_exe(self) -> str | None:
        for exe in ("python", "py", "python3"):
            try:
                subprocess.run([exe, "--version"], capture_output=True, timeout=8, **proc_quiet())
                return exe
            except Exception:
                continue
        return None

    def _detect_backend(self, base: Path):
        """Retorna ('django'|'flask'|'node'|None, alvo) pra saber como rodar o projeto."""
        if (base / "manage.py").exists():
            return "django", base / "manage.py"
        for py in base.glob("*.py"):
            try:
                txt = py.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if "Flask(" in txt or "from flask" in txt or "import flask" in txt:
                return "flask", py
            if "FastAPI(" in txt or "from fastapi" in txt:
                return "fastapi", py
        if (base / "package.json").exists():
            return "node", base / "package.json"
        return None, None

    def _open_preview(self, base: Path) -> None:
        """Preview inteligente: site estatico abre no navegador; projeto backend
        (Django/Flask/FastAPI/Node) SOBE O SERVIDOR e abre o localhost — nunca abre
        um template cru (que mostraria {% %} na tela)."""
        kind, target = self._detect_backend(base)
        idx = base / "index.html"
        # Site estatico de verdade: index.html sem tags de template e sem backend.
        if not kind and idx.exists():
            try:
                txt = idx.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                txt = ""
            if "{%" in txt or "{{" in txt:
                self._msg("sys", "⚠️ Esse index.html é um template (tem {% %}). Precisa de um servidor "
                          "pra renderizar — me diga o framework ou rode o servidor do projeto.", store=False)
                return
            try:
                webbrowser.open(idx.as_uri())
            except Exception:
                try:
                    subprocess.Popen(f'start "" "{idx}"', shell=True)
                except Exception:
                    pass
            return
        if kind:
            threading.Thread(target=self._serve_project, args=(base, kind, target), daemon=True).start()

    def _wait_port(self, host: str, port: int, timeout: float, proc=None) -> bool:
        """Espera a porta responder de verdade (ou o processo morrer)."""
        import socket
        end = time.time() + timeout
        while time.time() < end:
            try:
                with socket.create_connection((host, port), timeout=1):
                    return True
            except Exception:
                if proc is not None and proc.poll() is not None:
                    return False   # o servidor morreu antes de subir
                time.sleep(0.4)
        return False

    def _serve_and_open(self, cmd: list, base: Path, port: int, label: str, env=None):
        """Sobe um servidor, ESPERA a porta abrir e só então abre o navegador.
        Retorna (ok, log). Em falha, encerra o processo e devolve o erro capturado."""
        log = base / "_kemy_server.log"
        try:
            fh = open(log, "wb")
        except Exception:
            fh = subprocess.DEVNULL
        p = subprocess.Popen(cmd, cwd=str(base), stdout=fh, stderr=subprocess.STDOUT,
                             env=env, **proc_quiet())
        self._servers.append(p)
        url = f"http://127.0.0.1:{port}"
        if self._wait_port("127.0.0.1", port, 30, p):
            webbrowser.open(url)
            self._msg("sys", f"✅ {label} no ar: {url}", store=False)
            return True, ""
        try:
            p.terminate()
        except Exception:
            pass
        tail = ""
        try:
            tail = log.read_text(encoding="utf-8", errors="ignore")[-1500:]
        except Exception:
            pass
        return False, tail

    def _autofix_server(self, base: Path, kind: str, log_tail: str) -> bool:
        """Lê o erro do servidor + arquivos do projeto e pede pro modelo CORRIGIR. Aplica os
        arquivos/edicoes corrigidos. Retorna True se mudou algo (vale tentar subir de novo)."""
        if not log_tail.strip():
            return False
        self._msg("sys", "🔧 Lendo o erro e corrigindo o projeto…", store=False)
        files_ctx = relevant_project_files(base, log_tail) or read_project_files(base)
        sysp = (SYSTEM_PROMPT + "\n\n=== CONSERTO DE SERVIDOR (" + kind + ") ===\n"
                "O servidor do projeto NAO subiu. Abaixo o ERRO exato e os ARQUIVOS atuais. "
                "Descubra a CAUSA (config errada, import, template/base faltando, rota, model, "
                "INSTALLED_APPS, settings) e CORRIJA. Reentregue SOMENTE os arquivos alterados em "
                "blocos <<<FILE: caminho>>>...<<<END>>> (ou edicoes <<<EDIT>>>). Sem explicacao.")
        user = (f"ERRO DO SERVIDOR:\n{log_tail}\n\nARQUIVOS ATUAIS DO PROJETO:\n{files_ctx}")
        try:
            reply = self.llm.chat(sysp, [{"role": "user", "content": user}], max_tokens=16000)
        except Exception as e:
            self._msg("sys", f"Não consegui gerar a correção ({e}).", store=False)
            return False
        files, _ = parse_llm_files(reply)
        edits = parse_edits(reply)
        if edits:
            self._apply_edits(edits, base)
        if files:
            self._save(files, base)
        return bool(files or edits)

    def _autofix_buttons(self, base: Path, kind: str, issues: list) -> bool:
        """Liga os botoes/links mortos: manda a IA criar rotas/views/forms/templates pra cada
        acao (criar/editar/excluir) funcionar e persistir. Retorna True se mudou algo."""
        if not issues:
            return False
        self._msg("sys", f"🔌 Ligando {len(issues)} botão(ões)/link(s) que estavam sem ação…", store=False)
        files_ctx = read_project_files(base)
        sysp = (SYSTEM_PROMPT + "\n\n=== LIGAR BOTOES/CRUD (" + kind + ") ===\n"
                "Os botoes/links abaixo NAO funcionam (sem rota/acao). Faca o CRUD COMPLETO funcionar: "
                "crie as rotas (urls), as views (GET mostra formulario / POST salva no banco), os ModelForm "
                "e os templates de formulario, e os de editar/excluir (com confirmacao). A lista deve "
                "atualizar apos salvar. PERSISTA no banco (SQLite no Django). Reentregue SOMENTE os arquivos "
                "alterados/novos em blocos <<<FILE: caminho>>>...<<<END>>>. Sem explicacao.")
        user = "BOTOES/LINKS SEM ACAO:\n- " + "\n- ".join(issues) + "\n\nARQUIVOS ATUAIS:\n" + files_ctx
        try:
            reply = self.llm.chat(sysp, [{"role": "user", "content": user}], max_tokens=16000)
        except Exception as e:
            self._msg("sys", f"Não consegui ligar os botões agora ({e}).", store=False)
            return False
        files, _ = parse_llm_files(reply)
        edits = parse_edits(reply)
        if edits:
            self._apply_edits(edits, base)
        if files:
            self._save(files, base)
        return bool(files or edits)

    def _sanitize_python(self, base: Path) -> None:
        """Corrige erros de sintaxe deterministicos nos .py gerados (ex.: zero a esquerda)."""
        try:
            pys = list(base.rglob("*.py"))
        except Exception:
            return
        for p in pys[:200]:
            try:
                src = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            fixed, changed = fix_py_leading_zeros(src)
            if changed:
                try:
                    p.write_text(fixed, encoding="utf-8")
                except Exception:
                    pass

    def _serve_project(self, base: Path, kind: str, target: Path) -> None:
        """Sobe o servidor, esperando subir DE VERDADE. Se cair, lê o erro, corrige e tenta
        de novo (loop subir-e-corrigir) — até entregar rodando."""
        try:
            # Passa credenciais de banco (Supabase) pro projeto, se o usuario configurou.
            child_env = dict(os.environ)
            for k in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "DATABASE_URL"):
                v = (getattr(self, "env_vars", {}) or {}).get(k) or (getattr(self, "env_vars", {}) or {}).get("KIMI_" + k)
                if v:
                    child_env[k] = v
            py = None
            if kind == "node":
                node = None
                for exe in ("npm", "npm.cmd"):
                    try:
                        subprocess.run([exe, "--version"], capture_output=True, timeout=8, **proc_quiet()); node = exe; break
                    except Exception:
                        continue
                if not node:
                    self._msg("sys", "📦 É um projeto Node. Instale o Node.js e rode: npm install && npm start", store=False)
                    return
                self._msg("sys", "🚀 Projeto Node — instalando deps e subindo o servidor…", store=False)
                subprocess.run([node, "install"], cwd=str(base), capture_output=True, timeout=400, **proc_quiet())
                cmd, port, label, env = [node, "start"], 3000, "servidor Node", child_env
            else:
                py = self._python_exe()
                if not py:
                    self._msg("sys", "🐍 É um projeto Python. Instale o Python e rode os comandos do README.", store=False)
                    return
                req = base / "requirements.txt"
                if req.exists():
                    subprocess.run([py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=str(base),
                                   capture_output=True, timeout=400, **proc_quiet())
                if kind == "django":
                    self._msg("sys", "🚀 Projeto Django — instalando, migrando e subindo…", store=False)
                    subprocess.run([py, "-m", "pip", "install", "django"], cwd=str(base), capture_output=True, timeout=300, **proc_quiet())
                    cmd, port, label, env = [py, "manage.py", "runserver", "--noreload", "127.0.0.1:8000"], 8000, "Django", child_env
                elif kind == "fastapi":
                    self._msg("sys", "🚀 Projeto FastAPI — subindo com uvicorn…", store=False)
                    subprocess.run([py, "-m", "pip", "install", "fastapi", "uvicorn"], cwd=str(base), capture_output=True, timeout=300, **proc_quiet())
                    cmd, port, label, env = [py, "-m", "uvicorn", f"{target.stem}:app", "--port", "8000"], 8000, "FastAPI", child_env
                else:  # flask
                    self._msg("sys", "🚀 Projeto Flask — subindo o servidor…", store=False)
                    subprocess.run([py, "-m", "pip", "install", "flask"], cwd=str(base), capture_output=True, timeout=300, **proc_quiet())
                    child_env["FLASK_APP"] = target.name
                    cmd, port, label, env = [py, "-m", "flask", "run", "--port", "5000"], 5000, "Flask", child_env

            if py:
                self._sanitize_python(base)   # conserta zero-a-esquerda e cia antes de subir
            # Auto-verificador de botoes: liga links/acoes mortos ANTES de mostrar (1 rodada).
            try:
                dead = audit_dead_controls(base)
                if dead and self._autofix_buttons(base, kind, dead) and py:
                    self._sanitize_python(base)
            except Exception:
                pass
            # Loop subir-e-corrigir (até 3 tentativas).
            for attempt in range(3):
                if py and kind == "django":
                    subprocess.run([py, "manage.py", "migrate"], cwd=str(base), capture_output=True, timeout=120, **proc_quiet())
                ok, tail = self._serve_and_open(cmd, base, port, label, env)
                if ok:
                    return
                if attempt < 2 and self._autofix_server(base, kind, tail):
                    if py:
                        self._sanitize_python(base)
                    self._msg("sys", f"🔁 Corrigi — tentando subir de novo (tentativa {attempt + 2})…", store=False)
                    continue
                extra = f"\n\n```\n{tail.strip()[-1000:]}\n```" if tail.strip() else ""
                self._msg("kemy", f"⚠️ O {label} não subiu mesmo após eu tentar corrigir.{extra}\n"
                          "Me diz o que você quer que eu ajuste que eu continuo.")
                return
        except Exception as exc:
            self._msg("sys", f"Não consegui subir o servidor automaticamente ({exc}). "
                      "Rode os comandos do README na pasta do projeto.", store=False)

    def _register_change(self, rel: str, old: str, new: str) -> dict:
        import difflib
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                         fromfile="antes", tofile="depois", lineterm=""))
        added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        key = uuid.uuid4().hex[:8]
        self._file_views[key] = {"path": rel, "added": added, "removed": removed,
                                 "diff": "\n".join(diff), "content": new}
        return {"key": key, "path": rel, "added": added, "removed": removed}

    def _show_chips(self, changed: list) -> None:
        if len(self._file_views) > 120:
            for k in list(self._file_views)[:-120]:
                self._file_views.pop(k, None)
        if changed:
            try:
                self._js(f"showFiles({json.dumps(changed)})")
            except Exception:
                pass

    def _apply_edits(self, edits: list, base: Path) -> None:
        """Edicao cirurgica: aplica trechos SEARCH->REPLACE nos arquivos existentes."""
        if not edits:
            return
        changed = []
        for e in edits:
            rel = str(e.get("path") or "").strip().lstrip("/\\")
            dest = base / rel
            if not rel:
                continue
            if not dest.exists():
                self._msg("sys", f"⚠️ {rel}: arquivo nao existe para editar (peca para criar primeiro).", store=False)
                continue
            try:
                old = dest.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            new, applied, failed = old, 0, 0
            for search, replace in e.get("edits", []):
                if search and search in new:
                    new = new.replace(search, replace, 1)
                    applied += 1
                elif search:
                    failed += 1
            if applied and new != old:
                try:
                    dest.write_text(new, encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                changed.append(self._register_change(rel, old, new))
            if failed:
                self._msg("sys", f"⚠️ {rel}: {failed} trecho(s) nao bateram com o codigo atual (nao alterei essa parte).", store=False)
        self._show_chips(changed)

    def get_file_view(self, key: str) -> dict:
        return self._file_views.get(key, {})

    def browse_project(self) -> None:
        """Abre o explorador de código (estilo VS Code): lista os arquivos do projeto atual."""
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        if not base.exists():
            self._msg("sys", "Esta conversa ainda não tem um projeto/arquivos.", store=False)
            return
        exts = (".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx", ".json", ".py", ".md",
                ".txt", ".csv", ".java", ".c", ".cpp", ".cs", ".php", ".rb", ".go", ".rs", ".sql", ".xml", ".yml")
        files = []
        try:
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("_"):
                    try:
                        size = p.stat().st_size
                    except Exception:
                        size = 0
                    files.append({"path": str(p.relative_to(base)).replace("\\", "/"), "size": size})
                if len(files) >= 200:
                    break
        except Exception:
            pass
        if not files:
            self._msg("sys", "Nenhum arquivo de código no projeto ainda.", store=False)
            return
        self._js(f"showProjectFiles({json.dumps(files)})")

    def read_project_file(self, rel: str) -> dict:
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        try:
            p = (base / rel).resolve()
            if base.resolve() not in p.parents and p != base.resolve():
                return {}
            content = p.read_text(encoding="utf-8", errors="ignore")
            return {"path": rel, "content": content[:200000], "lang": p.suffix.lower().lstrip(".")}
        except Exception:
            return {}

    def _maybe_run(self, commands: list[str], base: Path) -> None:
        if not commands:
            return
        try:
            base.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        def _is_safe_open(cmd: str) -> bool:
            c = cmd.strip().lower()
            return (c.startswith(("start ", "explorer ", "start\"", "cmd /c start"))
                    or c.startswith(("http://", "https://")))

        for cmd in commands:
            # abrir site/app e seguro -> roda sempre; instalar/apagar exige ⚡ Auto
            if not self.autonomous and not _is_safe_open(cmd):
                self._msg("sys", f"(comando sugerido, ative ⚡ Auto para rodar) $ {cmd}", store=False)
                continue
            run = cmd
            if run.strip().lower().startswith(("http://", "https://")):
                run = f'start "" "{run.strip()}"'
            self._msg("sys", f"$ {cmd}", store=False)
            try:
                p = subprocess.run(run, shell=True, cwd=str(base), capture_output=True, text=True, timeout=180)
                out = ((p.stdout or "") + (p.stderr or "")).strip()[:800]
                if out:
                    self._msg("sys", out, store=False)
            except Exception as exc:
                self._msg("sys", f"Falha: {exc}", store=False)

    def _after_speak(self) -> None:
        self._state("idle" if self.connected else "offline")
        if self.continuous and self.connected and not self.busy:
            threading.Timer(0.7, self.listen).start()

    def vts_connect(self) -> None:
        self._msg("sys", "Conectando ao VTube Studio…", store=False)
        self.vts.start()

    def set_voice(self, data: str) -> None:
        """Configura a voz de personagem (ElevenLabs): data = 'chave|voice_id'.
        Valida a chave de verdade e diz o erro exato se falhar."""
        key, _, voice = (data or "").partition("|")
        key, voice = key.strip(), voice.strip()
        if not key:
            self._msg("sys", "Cole a chave da API do ElevenLabs.", store=False)
            return
        voice = voice or self.speaker.el_voice
        self._msg("sys", "🎙️ Validando a chave do ElevenLabs…", store=False)
        threading.Thread(target=self._validate_voice, args=(key, voice), daemon=True).start()

    def _validate_voice(self, key: str, voice: str) -> None:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
        body = json.dumps({"text": "Oi, essa e a minha voz nova!",
                           "model_id": self.speaker.el_model,
                           "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                audio = r.read()
            if len(audio) < 256:
                raise RuntimeError("resposta vazia")
        except urllib.error.HTTPError as exc:
            reason = {401: "chave invalida ou sem permissao 'Text to Speech'",
                      403: "chave sem permissao 'Text to Speech' (ative no painel)",
                      404: "Voice ID nao encontrado (confira o ID da voz)",
                      422: "Voice ID invalido para sua conta",
                      429: "cota do mes esgotada"}.get(exc.code, f"erro HTTP {exc.code}")
            self._msg("sys", f"❌ ElevenLabs: {reason}. A voz NAO foi alterada.", store=False)
            return
        except Exception as exc:
            self._msg("sys", f"❌ Falha ao validar a voz: {exc}", store=False)
            return
        # sucesso: salva e toca o audio de teste
        cfg = config_dir() / ".env"
        self.speaker.el_key = key
        self.speaker.el_voice = voice
        self.speaker.available = True
        _set_env_var(cfg, "KEMY_ELEVENLABS_KEY", key)
        _set_env_var(cfg, "KEMY_ELEVENLABS_VOICE", voice)
        self.env_vars["KEMY_ELEVENLABS_KEY"] = key
        self.env_vars["KEMY_ELEVENLABS_VOICE"] = voice
        try:
            path = os.path.join(tempfile.gettempdir(), f"kemy_voicetest_{uuid.uuid4().hex[:6]}.mp3")
            with open(path, "wb") as f:
                f.write(audio)
            self.speaker._speaking = True
            self._state("speaking")
            self.speaker._play_mp3(path)
            self.speaker._speaking = False
            self._after_speak()
        except Exception:
            pass
        self._msg("sys", "✅ Voz personalizada ativada com sucesso!", store=False)

    def vts_test(self) -> None:
        if not self.vts.authed:
            self._msg("sys", "Conecte o VTube Studio primeiro (botao 🎭 VTuber).", store=False)
            return
        self._msg("sys", f"Testando a boca no parametro '{self.vts.mouth_param}' — olhe a janela do VTS…", store=False)
        threading.Thread(target=self.vts.test, daemon=True).start()

    def check_update(self) -> None:
        threading.Thread(target=self._do_update, daemon=True).start()

    def _do_update(self) -> None:
        try:
            req = urllib.request.Request(RELEASE_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "KemyDesktop"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
            # ja esta na ultima versao? compara o sha local com o do release
            remote = ""
            m = re.search(r"sha:\s*([0-9a-fA-F]{7,40})", data.get("body") or "")
            if m:
                remote = m.group(1)
            local = build_tag()
            if remote and local and (remote.startswith(local) or local.startswith(remote)):
                self._msg("sys", f"✅ Voce ja esta na versao mais recente ({local}). Nada a baixar.", store=False)
                self._js("setUpdate(false)")
                return
            url = next((a.get("browser_download_url") for a in (data.get("assets") or []) if str(a.get("name", "")).lower().endswith(".zip")), None)
            if not url:
                raise RuntimeError("Release sem .zip")
            if not getattr(sys, "frozen", False):
                self._msg("sys", "Update automatico so no .exe. Abrindo a pagina de Releases…", store=False)
                webbrowser.open(RELEASES_URL)
                return
            self._msg("sys", f"⬇ Baixando atualizacao {remote[:7] or ''} (~120MB)… pode levar 1-2 min.", store=False)
            tmp = Path(tempfile.mkdtemp(prefix="kemy_upd_"))
            zp = tmp / "u.zip"

            last = [0]

            def _progress(blocks, bsize, total):
                if total > 0:
                    pct = int(min(100, blocks * bsize * 100 / total))
                    if pct >= last[0] + 25:
                        last[0] = pct
                        self._msg("sys", f"… {pct}%", store=False)

            self._msg("sys", "📦 Baixando… (nao feche o app)", store=False)
            # Extrai a build nova numa pasta IRMA (mesmo disco -> troca atomica, sem misturar arquivos).
            token = uuid.uuid4().hex[:6]
            new_dir = EXE_DIR.parent / f"kemy_new_{token}"
            old_dir = EXE_DIR.parent / f"kemy_old_{token}"
            urllib.request.urlretrieve(url, zp, _progress)
            self._msg("sys", "📦 Download concluido. Extraindo…", store=False)
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(new_dir)
            # se o zip tiver uma subpasta unica, usa ela como raiz
            entries = [p for p in new_dir.iterdir()]
            if len(entries) == 1 and entries[0].is_dir() and not (new_dir / "KemyDesktop.exe").exists():
                new_dir = entries[0]
            self._msg("sys", "🔄 Trocando para a versao nova e reiniciando…", store=False)
            app = str(EXE_DIR)
            exe = str(EXE_DIR / "KemyDesktop.exe")
            bat = Path(tempfile.gettempdir()) / f"kemy_update_{token}.bat"
            # Troca de pasta inteira (atomica). Se nao conseguir mover a pasta atual (arquivo
            # travado), reabre a versao ATUAL intacta em vez de deixar quebrada.
            script = (
                "@echo off\r\n"
                "timeout /t 3 /nobreak >nul\r\n"
                f'move "{app}" "{old_dir}" >nul 2>&1\r\n'
                f'if exist "{app}" ( timeout /t 2 /nobreak >nul & move "{app}" "{old_dir}" >nul 2>&1 )\r\n'
                f'if exist "{app}" ( timeout /t 3 /nobreak >nul & move "{app}" "{old_dir}" >nul 2>&1 )\r\n'
                f'if exist "{app}" goto fallback\r\n'
                f'move "{new_dir}" "{app}" >nul 2>&1\r\n'
                f'start "" "{exe}"\r\n'
                f'rmdir /s /q "{old_dir}" >nul 2>&1\r\n'
                f'if exist "{old_dir}" ( timeout /t 2 /nobreak >nul & rmdir /s /q "{old_dir}" >nul 2>&1 )\r\n'
                f'if exist "{old_dir}" ( timeout /t 3 /nobreak >nul & rmdir /s /q "{old_dir}" >nul 2>&1 )\r\n'
                'del "%~f0"\r\n'
                'goto :eof\r\n'
                ':fallback\r\n'
                f'rmdir /s /q "{new_dir}" >nul 2>&1\r\n'
                f'start "" "{exe}"\r\n'
                'del "%~f0"\r\n'
            )
            bat.write_text(script, encoding="utf-8")
            subprocess.Popen(["cmd", "/c", str(bat)], creationflags=0x00000008)
            time.sleep(0.6)
            os._exit(0)
        except Exception as exc:
            self._msg("sys", f"Falha no update: {exc}. Abrindo a pagina de Releases…", store=False)
            try:
                webbrowser.open(RELEASES_URL)
            except Exception:
                pass


MINI_HTML = """<!doctype html><html><head><meta charset=utf-8><style>
html,body{margin:0;height:100%;background:#070a12;overflow:hidden;font-family:'Segoe UI',sans-serif}
.wrap{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;cursor:pointer;-webkit-user-select:none}
.face{width:120px;height:120px;border-radius:50%;background:radial-gradient(circle at 50% 36%,rgba(124,92,255,.30),rgba(12,8,20,.92) 72%);box-shadow:0 0 22px rgba(124,92,255,.5);display:flex;align-items:center;justify-content:center;animation:bob 4s ease-in-out infinite;transition:box-shadow .3s}
.face svg{width:106px;height:106px}
.iris{fill:#36e0c8}.gem{fill:#36e0c8}
.mouth{fill:#6e2440;transform-box:fill-box;transform-origin:center;transition:transform .04s linear}
.lbl{margin-top:7px;font-size:12px;color:#bfa9ff;opacity:.9}
@keyframes bob{0%,100%{transform:translateY(0)}50%{transform:translateY(-5px)}}
body.speaking .face{box-shadow:0 0 32px rgba(54,246,214,.65)}
body.listening .face{box-shadow:0 0 30px rgba(255,91,138,.6)}
body.thinking .face{animation:bob 1.1s ease-in-out infinite}
</style></head><body class=idle>
<div class=wrap onclick="pywebview.api.show_main()" title="Clique para abrir a Kemy">
  <div class=face>
    <svg viewBox="0 0 200 200" aria-hidden="true">
      <path d="M40 96 Q28 180 70 168 Q100 182 130 168 Q172 180 160 96 Q150 30 100 26 Q50 30 40 96Z" fill="#1a1030"/>
      <ellipse cx="100" cy="104" rx="42" ry="50" fill="#ece1f2"/>
      <path d="M55 96 Q44 36 100 30 Q156 36 145 96 Q132 70 116 78 Q100 104 84 78 Q68 70 55 96Z" fill="#211433"/>
      <path d="M100 64 l6 7 l-6 8 l-6 -8 Z" class="gem"/>
      <path d="M74 100 q10 -7 22 -2" stroke="#120a1f" stroke-width="3" fill="none" stroke-linecap="round"/>
      <path d="M104 98 q12 -5 22 2" stroke="#120a1f" stroke-width="3" fill="none" stroke-linecap="round"/>
      <g class="eye"><ellipse cx="85" cy="110" rx="9" ry="10" fill="#f3ecff"/><circle class="iris" cx="85" cy="110" r="5.5"/><circle cx="87" cy="108" r="1.8" fill="#fff"/></g>
      <g class="eye"><ellipse cx="115" cy="110" rx="9" ry="10" fill="#f3ecff"/><circle class="iris" cx="115" cy="110" r="5.5"/><circle cx="117" cy="108" r="1.8" fill="#fff"/></g>
      <ellipse cx="80" cy="126" rx="6" ry="3" fill="#b06a8f" opacity=".6"/>
      <ellipse cx="120" cy="126" rx="6" ry="3" fill="#b06a8f" opacity=".6"/>
      <ellipse class="mouth" cx="100" cy="136" rx="7" ry="5"/>
    </svg>
  </div>
  <div class=lbl id=lbl>Kemy</div>
</div>
<script>
function kemyState(s){document.body.className=s;document.getElementById('lbl').textContent={idle:'Kemy',listening:'Ouvindo…',thinking:'Pensando…',speaking:'Falando…'}[s]||s;}
var _mouth=document.querySelector('.mouth');
function kemyMouth(v){ if(_mouth){ var s=1+Math.max(0,Math.min(1,v))*2.0; _mouth.style.transform='scaleY('+s+')'; } }
</script></body></html>"""


def _corner_pos(w: int, h: int):
    """Canto inferior direito, acima da barra de tarefas (perto do relogio)."""
    try:
        import ctypes
        u = ctypes.windll.user32
        try:
            u.SetProcessDPIAware()
        except Exception:
            pass
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        return max(0, sw - w - 24), max(0, sh - h - 64)
    except Exception:
        return 1120, 560


def _start_tray(api, win) -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        return
    img = Image.new("RGBA", (64, 64), (11, 15, 26, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=(45, 224, 200, 255))
    d.ellipse((22, 26, 30, 34), fill=(11, 15, 26, 255))
    d.ellipse((36, 26, 44, 34), fill=(11, 15, 26, 255))
    menu = pystray.Menu(
        pystray.MenuItem("Mostrar Kemy", lambda i: api.show_main(), default=True),
        pystray.MenuItem("Esconder janela", lambda i: api.hide_main()),
        pystray.MenuItem("Falar agora", lambda i: api.listen()),
        pystray.MenuItem("Sair", lambda i: (i.stop(), api.quit_app())),
    )
    icon = pystray.Icon("Kemy", img, "Kemy", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    api._tray = icon


def run_webview(host: str, port: int) -> bool:
    """Tenta a UI moderna em HTML. Retorna False se pywebview nao estiver disponivel."""
    try:
        import webview  # noqa
    except Exception as exc:
        _record_webview_error(f"import webview falhou: {exc!r}")
        return False
    html = _find_ui_html()
    if not html:
        _record_webview_error("ui.html nao encontrado no bundle")
        return False
    api = WebApi(host, port)
    api._start_obs()   # sobe o servidor do avatar ANTES, pro mascote/OBS ja terem a URL
    win = webview.create_window(f"Kemy - Assistente ({build_tag()})", url=html.as_uri(), js_api=api,
                                width=1100, height=780, min_size=(280, 360),
                                background_color="#070a12")
    api.window = win

    # fechar a janela apenas esconde (app continua na bandeja + mini avatar)
    def _on_closing():
        if api._quitting:
            return True
        api.hide_main()
        return False

    try:
        win.events.closing += _on_closing
    except Exception:
        pass

    # A bandeja e criada SO depois que a janela principal termina de carregar.
    # (O modo Mini foi removido — era a causa principal de travamento.)
    def _setup_companion():
        if getattr(api, "_companion_done", False):
            return
        api._companion_done = True
        # Mascote flutuante no desktop: janela transparente, sem moldura, sempre no topo,
        # carregando a MESMA pagina do avatar (sincroniza fala). Criada UMA vez, apos a
        # janela principal carregar (evita o conflito de 2 janelas WebView2 no boot).
        url = api.obs_url()
        if url and os.environ.get("KEMY_PET", "1") != "0":
            mw, mh = 280, 340
            mx, my = _corner_pos(mw, mh)
            pet = None
            # Tenta transparente; se o WebView2 nao suportar (erro), refaz SEM transparencia
            # pra pelo menos abrir a janela do mascote.
            for kw in ({"transparent": True}, {"background_color": "#070a12"}):
                try:
                    pet = webview.create_window(
                        "Kemy", url=url + "?nolabel=1",
                        width=mw, height=mh, x=mx, y=my,
                        frameless=True, easy_drag=True, on_top=True, **kw)
                    break
                except Exception:
                    pet = None
                    continue
            api.pet_win = pet
            api._pet_visible = bool(pet)
        try:
            _start_tray(api, win)
        except Exception:
            pass

    try:
        win.events.loaded += lambda: _setup_companion()
    except Exception:
        pass

    webview.start()
    try:
        api.speaker.stop()
    except Exception:
        pass
    return True


def webview_open_dialog():
    import webview
    return webview.OPEN_DIALOG


def webview_folder_dialog():
    import webview
    return webview.FOLDER_DIALOG


def _set_env_var(path: Path, key: str, value: str) -> None:
    """Cria/atualiza uma linha KEY=VALUE num arquivo .env, preservando o resto."""
    lines: list[str] = []
    try:
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    out, done = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}=") or ln.strip().startswith(f"{key} ="):
            out.append(f"{key}={value}")
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(f"{key}={value}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kemy Desktop - assistente de voz local")
    parser.add_argument("--serve", action="store_true", help="Executa apenas o backend FastAPI.")
    parser.add_argument("--classic", action="store_true", help="Forca a UI antiga (Tkinter).")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.serve:
        run_server(args.host, args.port)
        return 0
    os.chdir(ROOT_DIR)
    cleanup_update_leftovers()
    # Propaga o .env (incl. voz: KEMY_VOICE/KEMY_ELEVENLABS_*) pro os.environ — sem isso o
    # Speaker nao via a voz personalizada configurada no .env.
    try:
        for _k, _v in load_merged_env().items():
            if _v:
                os.environ[_k] = _v
    except Exception:
        pass
    # UI moderna (HTML/pywebview); cai para Tkinter se indisponivel ou --classic.
    if not args.classic:
        unblock_bundle()
        try:
            if run_webview(args.host, args.port):
                return 0
        except Exception:
            import traceback
            _record_webview_error(traceback.format_exc())
    root = tk.Tk()
    app = KemyVoiceApp(root, host=args.host, port=args.port)
    root.mainloop()
    _ = app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
