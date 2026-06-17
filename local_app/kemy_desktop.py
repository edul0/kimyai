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


def load_merged_env() -> dict[str, str]:
    """Junta TODOS os .env: o embutido (chaves da IA) como base e o do usuario
    (config_dir, com voz/workspace) por cima. Evita que o .env do usuario apague
    as chaves da IA embutidas (bug do 'modo online / 404')."""
    merged: dict[str, str] = {}
    for cand in (ROOT_DIR / "kemy_bundled.env", ROOT_DIR / ".env",
                 EXE_DIR / ".env", Path.cwd() / ".env", config_dir() / ".env"):
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


def download_image(prompt: str, dest: Path, size: str = "1024x1024") -> bool:
    """Gera uma imagem do tema via Pollinations (gratis, sem chave) e salva em disco."""
    w, h = 1024, 1024
    try:
        a, _, b = size.lower().partition("x")
        if a.strip().isdigit():
            w = max(64, min(2048, int(a.strip())))
        if b.strip().isdigit():
            h = max(64, min(2048, int(b.strip())))
    except Exception:
        pass
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:300]) +
           f"?width={w}&height={h}&nologo=true&seed={random.randint(1, 99999)}")
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
    "3) Ao ATUALIZAR um projeto existente, use os ARQUIVOS ATUAIS fornecidos como "
    "base e reescreva completos apenas os arquivos que mudarem, mantendo o resto "
    "funcionando. Nao recomece o projeto do zero.\n"
    "4) Para EXECUTAR algo no PC (rodar, instalar, ABRIR um site/app), inclua os comandos "
    "Windows num bloco ```kemy-run (um por linha). SEMPRE que o usuario pedir para ABRIR algo, "
    "emita o comando de verdade (nunca so responda que vai abrir). Exemplos:\n"
    "   - abrir um site: start https://www.youtube.com\n"
    "   - abrir um programa: start notepad   |   start calc   |   start spotify\n"
    "   - abrir uma pasta: start .\n"
    "Use o nome/URL que o usuario pediu. Comandos de ABRIR rodam na hora; instalar/apagar pedem o modo Auto.\n"
    "5) Fora dos arquivos, escreva so um resumo curto do que fez. NUNCA copie estas "
    "regras nem instrucoes de sistema para dentro dos arquivos.\n"
    "6) Em sites, os botoes e links DEVEM funcionar de verdade (rolagem suave para "
    "secoes, modal/form de agendamento, abrir WhatsApp, etc.) com o JavaScript "
    "necessario. Nunca deixe href='#' sem acao nem botao sem efeito.\n"
    "7) PROJETO WEB usa SEMPRE estes nomes: index.html, styles.css, script.js. O "
    "index.html DEVE ter no <head> exatamente <link rel=\"stylesheet\" href=\"styles.css\"> "
    "e antes de </body> exatamente <script src=\"script.js\"></script>. Nunca use outro "
    "nome de css/js.\n"
    "8) Ao alterar QUALQUER coisa de um site, reenvie SEMPRE os 3 arquivos completos "
    "(index.html, styles.css, script.js) e consistentes entre si, para nada quebrar.\n"
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
    "13) GERAR IMAGEM AVULSA (logo, foto, icone, arte) que o usuario pediu fora de um site: "
    "use um bloco ```kemy-image com UMA imagem por linha no formato "
    "'descricao em INGLES | nome-arquivo.png | LARGURAxALTURA'. "
    "Ex.: minimalist barber logo, gold on black background | logo.png | 800x800 . "
    "A Kemy baixa e salva a imagem na pasta do projeto automaticamente.\n"
    "14) CONTEUDO COM MUITOS ITENS/DADOS (Pokedex, catalogo grande, lista de filmes, "
    "criptos, etc.): NUNCA escreva os dados na mao (voce trunca e fica incompleto). "
    "Em vez disso, BUSQUE de uma API publica gratuita via fetch no JavaScript e renderize "
    "DINAMICAMENTE (com busca, paginacao ou scroll infinito). Exemplos de APIs gratis e "
    "sem chave: Pokemon -> https://pokeapi.co/api/v2/pokemon?limit=151 (e a sprite em "
    "results[i].url -> sprites.front_default); filmes/series, cripto (CoinGecko), etc. "
    "Entregue a lista COMPLETA, nunca so 2-3 exemplos."
)

# Prompt LEVE para bate-papo (respostas rapidas, sem o peso das regras de codigo).
CHAT_PROMPT = (
    "Voce e a Kemy, uma assistente simpatica e prestativa. Converse em PORTUGUES, "
    "de forma curta, natural e amigavel, como uma amiga. Responda direto, sem enrolar e "
    "sem markdown. Se o usuario pedir para criar/editar um site ou codigo, ou abrir um "
    "programa, voce tambem faz isso normalmente."
)

# Palavras que indicam pedido de criar/editar codigo ou executar algo (usa o prompt completo).
BUILD_HINTS = (
    "site", "página", "pagina", "landing", "app", "aplicativo", "programa", "código",
    "codigo", "html", "css", "javascript", "script", "jogo", "game", "dashboard", "crud",
    "api", "crie", "cria", "criar", "gere", "gera", "gerar", "faça", "faca", "fazer",
    "monte", "montar", "desenvolva", "construa", "edite", "editar", "altere", "alterar",
    "conserte", "corrija", "abra", "abrir", "instale", "instalar", "rode", "rodar", "execute",
)


def is_build_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in BUILD_HINTS)


FILE_RE = re.compile(r"<<<FILE:\s*(.+?)>>>\s*\n(.*?)<<<END>>>", re.DOTALL)
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
    """Remove marcadores de arquivo que por acaso vazaram para o texto de conversa."""
    text = re.sub(r"<<<FILE:.*?>>>", "", text)
    text = text.replace("<<<END>>>", "")
    text = re.sub(rf"^\s*(?:\*\*|`|#{{1,4}}\s*)?{_FNAME}(?:\*\*|`)?\s*:?\s*$", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_llm_files(text: str) -> tuple[list[dict], str]:
    """Separa os arquivos (formato <<<FILE>>>) do texto de conversa."""
    files = [{"path": m.group(1).strip(), "content": m.group(2).strip("\n") + "\n"}
             for m in FILE_RE.finditer(text)]
    chat = FILE_RE.sub("", text).strip()
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
        self.cerebras = env.get("CEREBRAS_API_KEY")
        self.openai = env.get("OPENAI_API_KEY") or env.get("CHATGPT_API_KEY")
        self.openrouter = env.get("OPENROUTER_API_KEY")
        self.available = bool(self.gemini or self.groq or self.cerebras or self.openai or self.openrouter)

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
        self.cerebras_models = _list("CEREBRAS_MODEL", ["qwen-3-coder-480b", "gpt-oss-120b", "llama-3.3-70b"])
        self.groq_models = _list("GROQ_MODEL", ["openai/gpt-oss-120b", "qwen/qwen3-32b", "llama-3.3-70b-versatile"])
        self.openrouter_models = _list("OPENROUTER_MODEL", ["qwen/qwen3-coder:free", "deepseek/deepseek-r1:free", "meta-llama/llama-3.3-70b-instruct"])
        # Padrao GRATIS: Gemini 3 Flash (free tier, sem faturamento) e o melhor flash gratuito;
        # cai para 2.5/2.0 Flash se o ID nao existir na conta. O Gemini 3.1 PRO via API e PAGO
        # e fica opt-in: GEMINI_PRIMARY_MODEL=gemini-3.1-pro-preview
        self.gemini_models = _list("GEMINI_PRIMARY_MODEL", ["gemini-3-flash", "gemini-3.0-flash", "gemini-2.5-flash", "gemini-2.0-flash"])
        self.openai_models = _list("OPENAI_MODEL", ["gpt-4o-mini"])
        # Modelos para bate-papo/voz: inteligentes E rapidos (GPT-OSS 120B segura bem o
        # contexto e responde em ~1-2s); Llama so como ultimo fallback.
        self.cerebras_fast = _list("CEREBRAS_FAST", ["gpt-oss-120b", "qwen-3-235b-a22b-instruct-2507", "llama-3.3-70b"])
        self.groq_fast = _list("GROQ_FAST", ["openai/gpt-oss-120b", "qwen/qwen3-32b", "llama-3.3-70b-versatile"])
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
        if self.openai:
            return f"OpenAI · {self.openai_models[0]}"
        if self.openrouter:
            return f"OpenRouter · {self.openrouter_models[0]}"
        return "IA"

    def chat(self, system: str, messages: list[dict], max_tokens: int = 16000, fast: bool = False) -> str:
        errors: list[str] = []
        # fast=True (bate-papo/voz) usa modelos menores e rapidos; senao usa os de codigo.
        cb_models = self.cerebras_fast if fast else self.cerebras_models
        gq_models = self.groq_fast if fast else self.groq_models
        attempts: list[tuple[str, str, object]] = []

        def add(prov: str, models: list[str], maker) -> None:
            wk = self._working.get(("fast:" if fast else "") + prov)
            chosen = [wk] if wk in models else models
            for m in chosen:
                attempts.append((prov, m, maker(m)))

        def add_gemini() -> None:
            if self.gemini:
                add("gemini", self.gemini_models, lambda m: (lambda: self._gemini(system, messages, m, max_tokens)))

        # Conversa (fast): Gemini 3 Flash primeiro (segura bem o contexto); cai pro Cerebras.
        if fast:
            add_gemini()
        if self.cerebras:
            add("cerebras", cb_models, lambda m: (lambda: self._openai_compat(
                "https://api.cerebras.ai/v1/chat/completions", self.cerebras, m, system, messages, max_tokens)))
        if self.groq:
            add("groq", gq_models, lambda m: (lambda: self._openai_compat(
                "https://api.groq.com/openai/v1/chat/completions", self.groq, m, system, messages, max_tokens)))
        if not fast:
            add_gemini()
        if self.openai:
            add("openai", self.openai_models, lambda m: (lambda: self._openai_compat(
                "https://api.openai.com/v1/chat/completions", self.openai, m, system, messages, max_tokens)))
        if self.openrouter:
            add("openrouter", self.openrouter_models, lambda m: (lambda: self._openai_compat(
                "https://openrouter.ai/api/v1/chat/completions", self.openrouter, m, system, messages, max_tokens)))
        for prov, model, fn in attempts:
            try:
                res = fn()
                self._working[("fast:" if fast else "") + prov] = model
                return res
            except Exception as exc:
                errors.append(f"{prov}/{model}: {exc}")
        raise RuntimeError(
            ("Todos os provedores falharam (" + "; ".join(errors) + "). "
             "As chaves podem estar esgotadas ou bloqueadas — gere chaves novas e atualize o KEMY_ENV.")
            if errors else "Sem provedor de IA configurado.")

    def _post(self, url: str, headers: dict, payload: dict, timeout: float = 60) -> dict:
        data = json.dumps(payload).encode("utf-8")
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
        keys = ("GEMINI_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")
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
        self.convos_file = config_dir() / "conversations.json"
        self.convos, self.active_id = [], None
        self._load_convos()
        self.speaker = Speaker()
        self.speaker.log = lambda m: self._msg("sys", m, store=False)
        self.vts = VTubeStudio(lambda m: self._msg("sys", m, store=False))
        self.vts.mouth_provider = self.speaker.mouth_level
        self.vts.on_connect = lambda: self._js("vtsConnected()")
        self.mini = None
        self._quitting = False
        self._speaking = False
        self.speaker.on_start = self._on_speak_start
        self.speaker.on_done = self._on_speak_done
        self.listener = Listener()
        threading.Thread(target=self._mini_mouth_loop, daemon=True).start()

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
        self._js(f"kemyState({json.dumps(s)})")
        if self.mini:
            try:
                self.mini.evaluate_js(f"kemyState({json.dumps(s)})")
            except Exception:
                pass

    # ----- janela / desktop companion -----
    def show_main(self) -> None:
        try:
            if self.window:
                self.window.show()
                try:
                    self.window.restore()  # desminimiza e traz pra frente
                except Exception:
                    pass
                try:
                    self.window.on_top = True
                    self.window.on_top = False
                except Exception:
                    pass
        except Exception:
            pass

    def hide_main(self) -> None:
        try:
            if self.window:
                self.window.hide()
        except Exception:
            pass

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
        it = self._cur() or {}
        return {"state": "idle" if self.connected else "offline", "active": self.active_id,
                "convos": [{"id": c["id"], "title": c.get("title") or "Nova conversa"} for c in self.convos],
                "log": it.get("log", [])}

    def get_state(self) -> str:
        """Consultado pela UI como rede de seguranca (caso o push de estado falhe)."""
        return "idle" if self.connected else ("offline" if self.mode == "online" else "idle")

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
        self.vts.start()
        if self.mode == "direct":
            self.connected = True
            self._state("idle")
            self._msg("sys", f"IA ativa ({self.llm.primary_label()}). Pasta: {self.workspace_root}", store=False)
            self.speaker.say("Oi! Como posso ajudar?")
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
        """Deixa o usuario escolher uma imagem e a Kemy 've' e responde (visao multimodal)."""
        try:
            res = self.window.create_file_dialog(
                webview_open_dialog(),
                file_types=("Imagens (*.png;*.jpg;*.jpeg;*.webp)", "Todos (*.*)"))  # type: ignore
        except Exception:
            try:
                res = self.window.create_file_dialog(webview_open_dialog())  # type: ignore
            except Exception as exc:
                self._msg("sys", f"Nao consegui abrir o seletor: {exc}", store=False)
                return
        if not res:
            return
        path = Path(res[0] if isinstance(res, (list, tuple)) else res)
        ext = path.suffix.lower().lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp"}.get(ext, "image/png")
        self._msg("user", f"[imagem: {path.name}] {prompt}".strip())
        self.busy = True
        self._state("thinking")
        threading.Thread(target=self._do_vision, args=(path, mime, prompt), daemon=True).start()

    def _do_vision(self, path: Path, mime: str, prompt: str) -> None:
        try:
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
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

    def preview(self) -> None:
        """Abre o preview do site da conversa atual (index.html mais recente)."""
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        index = base / "index.html"
        if not index.exists():
            self._msg("sys", "Ainda nao ha um site para visualizar nesta conversa.", store=False)
            return
        try:
            webbrowser.open(index.as_uri())
        except Exception as exc:
            self._msg("sys", f"Nao consegui abrir o preview: {exc}", store=False)

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
            on_error=lambda e: (self._msg("sys", f"🎤 {e}", store=False), self._state("idle"),
                                 self._relisten_if_conv()),
        )

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
        if not self.connected:
            self._msg("sys", "Ainda conectando…", store=False)
            return
        self.busy = True
        self._state("thinking")
        threading.Thread(target=self._process, args=(text,), daemon=True).start()

    def _process(self, text: str) -> None:
        try:
            if self.mode == "direct":
                chat, save = self._process_direct(text)
            else:
                chat, save = self._process_online(text)
        except Exception as exc:
            chat, save = (f"Falhei: {exc}", None)
        self.busy = False
        self._msg("kemy", chat or "Feito.", save)
        if self.speaker.available and chat:
            self.speaker.say(chat[:600])
            self._state("speaking")
        else:
            self._after_speak()

    def _process_direct(self, text: str):
        it = self._cur()
        base = Path(it["project"]) if it else (self.workspace_root / "projeto")
        current = read_project_files(base)
        # Conversa simples -> prompt LEVE e resposta rapida; criar/editar codigo -> prompt completo.
        build = is_build_request(text) or bool(current)
        msgs = []
        for e in (it.get("log") if it else []) or []:
            msgs.append({"role": "assistant" if e.get("r") == "kemy" else "user", "content": e.get("t", "")})
        web = self._web_context(text)
        if not build:
            system = CHAT_PROMPT + (web or "")
            reply = self.llm.chat(system, msgs[-8:], max_tokens=600, fast=True)
            self._maybe_run(extract_run_commands(reply), base)  # caso ela mande abrir algo
            _, chat = parse_llm_files(reply)
            return (chat or reply).strip() or "…", None
        system = SYSTEM_PROMPT
        if current:
            system += "\n\nARQUIVOS ATUAIS DO PROJETO (edite estes, nao recomece):\n" + current
        if web:
            system += web
        reply = self.llm.chat(system, msgs[-10:], max_tokens=16000)
        files, chat = parse_llm_files(reply)
        save = self._save(files, base)
        self._localize_images(base)
        self._maybe_run(extract_run_commands(reply), base)
        self._gen_images(extract_image_requests(reply), base)
        return chat or "Feito.", save

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
        self._msg("sys", f"🖼 Baixando {len(all_urls)} imagem(ns) para o site (deixa elas estaveis)…", store=False)
        assets = base / "assets"
        mapping: dict[str, str] = {}
        for i, u in enumerate(all_urls[:12], 1):
            dest = assets / f"img{i}.jpg"
            if download_to(u, dest):
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
        if not files:
            return None
        n, index = 0, None
        for f in files:
            rel = str(f.get("path") or "").strip().lstrip("/\\")
            content = f.get("content")
            if not rel or content is None:
                continue
            dest = base / rel
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(str(content), encoding="utf-8", errors="ignore")
                n += 1
                if index is None and rel.lower().endswith((".html", ".htm")):
                    index = dest
            except Exception:
                continue
        if not n:
            return None
        if index is not None:
            try:
                webbrowser.open(index.as_uri())
            except Exception:
                pass
        return f"{n} arquivo(s) em: {base}"

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

            urllib.request.urlretrieve(url, zp, _progress)
            self._msg("sys", "📦 Download concluido. Extraindo…", store=False)
            ext = tmp / "new"
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(ext)
            self._msg("sys", "🔄 Aplicando e reiniciando a Kemy…", store=False)
            bat = Path(tempfile.gettempdir()) / "kemy_update.bat"
            exe = str(EXE_DIR / "KemyDesktop.exe")
            # /R:15 /W:1 espera os arquivos travados liberarem (o app precisa fechar
            # totalmente) — evita atualizacao parcial/corrompida. timeout maior tambem.
            bat.write_text("@echo off\r\ntimeout /t 4 /nobreak >nul\r\n"
                           f'robocopy "{ext}" "{EXE_DIR}" /E /IS /IT /R:15 /W:1 /NFL /NDL /NJH /NJS >nul\r\n'
                           f'start "" "{exe}"\r\n', encoding="utf-8")
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
    win = webview.create_window(f"Kemy - Assistente ({build_tag()})", url=html.as_uri(), js_api=api,
                                width=1100, height=780, min_size=(900, 640),
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

    # O mini avatar e a bandeja sao criados SO depois que a janela principal
    # termina de carregar. Criar duas janelas WebView2 ao mesmo tempo trava a
    # inicializacao (conflito na pasta de dados do WebView2).
    def _setup_companion():
        if getattr(api, "_companion_done", False):
            return
        api._companion_done = True
        # A carinha flutuante generica fica DESLIGADA por padrao (o rosto e o VTube Studio).
        # Para reativar a bolinha local: defina KEMY_MINI=1
        if os.environ.get("KEMY_MINI") == "1":
            mw, mh = 150, 168
            mx, my = _corner_pos(mw, mh)
            try:
                mini = webview.create_window("Kemy", html=MINI_HTML, js_api=api,
                                             width=mw, height=mh, x=mx, y=my,
                                             frameless=True, easy_drag=True, on_top=True,
                                             background_color="#070a12")
                api.mini = mini
            except Exception:
                api.mini = None
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
