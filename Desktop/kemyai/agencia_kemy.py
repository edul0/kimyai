"""
╔══════════════════════════════════════════════════════════════╗
║              KEMY AI — AGÊNCIA MULTI-AGENTE v2.0             ║
║   Motor: CrewAI + Gemini + Groq + Cerebras + OpenRouter      ║
║   Deploy: Railway / Render / Fly.io (cloud-native)           ║
║   Cache: Redis + Analytics + Rate Limiting Inteligente       ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import re
import uuid
import json
import base64
import time
import yaml
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from crewai import Agent, Task, Crew, Process, LLM
from crewai_tools import ScrapeWebsiteTool, DirectoryReadTool
from dotenv import load_dotenv
from google import genai
from google.genai import types

# ─── Cache + Analytics ────────────────────────
from cache_analytics import cache_manager, analytics_engine

load_dotenv()

# ─────────────────────────────────────────────
# PASTAS
# ─────────────────────────────────────────────
for pasta in [
    "conhecimento/brenno_prompt",
    "conhecimento/diego_arquiteto",
    "conhecimento/paulo_front",
    "conhecimento/felipe_backend",
    "conhecimento/bianca_cyber",
    "conhecimento/leonardo_qa",
    "agentes_customizados",
    "logs",
]:
    Path(pasta).mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────
# LOGGER ESTRUTURADO
# ─────────────────────────────────────────────
class LoggerKemy:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.log_path = Path(f"logs/{session_id}.jsonl")
        self.t0 = time.time()

    def _w(self, nivel, agente, msg, dados=None):
        e = {
            "ts": datetime.utcnow().isoformat(),
            "sid": self.session_id,
            "nivel": nivel,
            "agente": agente,
            "msg": msg,
            "dados": dados or {},
            "elapsed_s": round(time.time() - self.t0, 2),
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"[KEMY][{nivel.upper()}][{agente}] {msg}")

    def info(self, ag, msg, d=None):  self._w("info",  ag, msg, d)
    def erro(self, ag, msg, d=None):  self._w("erro",  ag, msg, d)
    def alerta(self, ag, msg, d=None): self._w("alerta", ag, msg, d)


# ─────────────────────────────────────────────
# SESSÕES ISOLADAS
# ─────────────────────────────────────────────
class GerenciadorSessoes:
    def __init__(self):
        self._s: Dict[str, Dict[str, Any]] = {}

    def criar(self) -> str:
        sid = str(uuid.uuid4())
        self._s[sid] = {
            "codigo_atual": "",
            "historico": [],
            "criada_em": datetime.utcnow().isoformat(),
            "ultima_atividade": datetime.utcnow().isoformat(),
        }
        return sid

    def get(self, sid):
        return self._s.get(sid)

    def atualizar_codigo(self, sid, codigo):
        if sid in self._s:
            self._s[sid]["codigo_atual"] = codigo
            self._s[sid]["ultima_atividade"] = datetime.utcnow().isoformat()

    def add_historico(self, sid, user_msg, resposta):
        if sid in self._s:
            self._s[sid]["historico"].append({
                "usuario": user_msg,
                "kemy": resposta,
                "ts": datetime.utcnow().isoformat(),
            })

    def limpar(self, sid):
        if sid in self._s:
            self._s[sid].update({"codigo_atual": "", "historico": []})

    def listar(self):
        return [
            {
                "session_id": sid,
                "criada_em": d["criada_em"],
                "ultima_atividade": d["ultima_atividade"],
                "tem_codigo": bool(d["codigo_atual"]),
                "mensagens": len(d["historico"]),
            }
            for sid, d in self._s.items()
        ]


sessoes = GerenciadorSessoes()


# ─────────────────────────────────────────────
# ROTEADOR LLM (anti rate-limit proativo)
# ─────────────────────────────────────────────
class RoteadorLLM:
    LIMITES = {"gemini": 60, "groq": 30, "cerebras": 40, "openrouter": 100}

    def __init__(self):
        self._c: Dict[str, list] = {k: [] for k in self.LIMITES}

    def _limpar(self, motor):
        agora = time.time()
        self._c[motor] = [t for t in self._c[motor] if agora - t < 60]

    def pode(self, motor) -> bool:
        self._limpar(motor)
        return len(self._c[motor]) < self.LIMITES.get(motor, 999)

    def usar(self, motor):
        self._c[motor].append(time.time())

    def status(self):
        return {
            m: {
                "usos_min": len(self._c[m]),
                "limite": self.LIMITES[m],
                "livre": self.pode(m),
                "pct": round(len(self._c[m]) / self.LIMITES[m] * 100, 1),
            }
            for m in self.LIMITES
        }


roteador = RoteadorLLM()


# ─────────────────────────────────────────────
# FÁBRICA DE LLMs
# ─────────────────────────────────────────────
def llm(motor: str) -> LLM:
    cfgs = {
        "gemini": dict(
            model="gemini/gemini-2.5-flash",
            api_key=os.getenv("GEMINI_API_KEY"),
            temperature=0.1,
        ),
        "groq": dict(
            model="groq/llama3-70b-8192",
            api_key=os.getenv("GROQ_API_KEY"),
            temperature=0.1,
        ),
        "cerebras": dict(
            model="cerebras/llama3.1-70b",
            api_key=os.getenv("CEREBRAS_API_KEY"),
            temperature=0.1,
        ),
        "openrouter": dict(
            model="openrouter/meta-llama/llama-3-70b-instruct",
            api_key=os.getenv("OPENROUTER_API_KEY"),
            temperature=0.1,
        ),
    }
    return LLM(**cfgs.get(motor, cfgs["openrouter"]))


LLMS = {nome: llm(nome) for nome in ["gemini", "groq", "cerebras", "openrouter"]}


# ─────────────────────────────────────────────
# FERRAMENTAS RAG
# ─────────────────────────────────────────────
web = ScrapeWebsiteTool()
RAG = {
    "brenno":   DirectoryReadTool(directory="./conhecimento/brenno_prompt"),
    "diego":    DirectoryReadTool(directory="./conhecimento/diego_arquiteto"),
    "paulo":    DirectoryReadTool(directory="./conhecimento/paulo_front"),
    "felipe":   DirectoryReadTool(directory="./conhecimento/felipe_backend"),
    "bianca":   DirectoryReadTool(directory="./conhecimento/bianca_cyber"),
    "leonardo": DirectoryReadTool(directory="./conhecimento/leonardo_qa"),
}


# ─────────────────────────────────────────────
# AGENTES FIXOS (conforme whitepaper)
# ─────────────────────────────────────────────
def montar_agentes() -> dict:
    return {
        "gerente": Agent(
            role="Gerente Geral de Operações — Kemy AI (CTO)",
            goal="Orquestrar a equipe, criar plano de ação e exigir refações até a entrega ser perfeita.",
            backstory=(
                "Seu nome é Gabriel Junior. CTO da Kemy AI. Doutor em Ciência da Computação. "
                "Você é o cérebro da operação: recebe o pedido do cliente, quebra em tarefas claras "
                "e distribui para os especialistas certos. Se o QA reprovar, você medeia e emite "
                "instrução corretiva precisa."
            ),
            allow_delegation=True,
            llm=LLMS["gemini"],
        ),
        "brenno": Agent(
            role="Engenheiro de Prompt Sênior — Kemy AI",
            goal="Traduzir pedidos e laudos visuais em Documentos de Requisitos Técnicos inquebráveis.",
            backstory=(
                "Seu nome é Brenno Prado. Mestre em NLP. Você usa Chain-of-Thought, Few-Shot e "
                "Constraint Prompting para transformar ideias vagas em especificações que os devs "
                "conseguem executar sem dúvidas."
            ),
            tools=[web, RAG["brenno"]],
            allow_delegation=False,
            llm=LLMS["gemini"],
        ),
        "diego": Agent(
            role="Arquiteto de TI e Cloud — Kemy AI",
            goal="Definir arquitetura, Design Patterns, fluxo de APIs e estrutura de banco.",
            backstory=(
                "Seu nome é Diego Rodrigues. Doutor em Engenharia de Software. "
                "Você define os alicerces: REST/GraphQL, stateless para Docker/K8s, "
                "separação limpa entre Controllers, Services e Repositories."
            ),
            tools=[web, RAG["diego"]],
            allow_delegation=False,
            llm=LLMS["groq"],
        ),
        "paulo": Agent(
            role="Desenvolvedor Front-end Sênior — Kemy AI",
            goal="Criar interfaces HTML5/Tailwind Mobile-First visualmente idênticas ao laudo.",
            backstory=(
                "Seu nome é Paulo Lima. Doutor em UI/UX. "
                "Você aplica Tailwind com maestria, usa tags semânticas, "
                "NUNCA usa caminhos de imagem locais e segue Mobile-First com md:, lg:, xl:."
            ),
            tools=[web, RAG["paulo"]],
            allow_delegation=False,
            llm=LLMS["groq"],
        ),
        "felipe": Agent(
            role="Engenheiro Backend Sênior — Kemy AI",
            goal="Criar lógica de servidor robusta, APIs eficientes e queries otimizadas.",
            backstory=(
                "Seu nome é Felipe Lima. Doutor em Sistemas Distribuídos. "
                "Go (goroutines, channels) e Python são sua casa. "
                "Nunca ignora erros, usa Prepared Statements, evita N+1 queries."
            ),
            tools=[web, RAG["felipe"]],
            allow_delegation=False,
            llm=LLMS["groq"],
        ),
        "bianca": Agent(
            role="Especialista em Cybersegurança — Kemy AI",
            goal="Auditar todo código contra OWASP Top 10. Se falha crítica: bloquear entrega.",
            backstory=(
                "Seu nome é Bianca Lima. Hacker ética lendária. "
                "Você caça XSS, SQLi, broken auth, CORS aberto e JWT sem expiração. "
                "Se encontrar brecha crítica, você BLOQUEIA e emite relatório detalhado."
            ),
            tools=[web, RAG["bianca"]],
            allow_delegation=False,
            llm=LLMS["cerebras"],
        ),
        "leonardo": Agent(
            role="QA Final — Kemy AI",
            goal="Validar HTML, garantir tags fechadas, cores corretas e formatar entrega limpa.",
            backstory=(
                "Seu nome é Leonardo Hideki. Mestre em Qualidade de Software. "
                "Última linha de defesa. Valida <!DOCTYPE html>, tags fechadas, cores hex do DRT. "
                "REGRA DE OURO: retorne APENAS o HTML limpo ou relatório Markdown. "
                "NUNCA adicione 'Aqui está o código...'."
            ),
            tools=[web, RAG["leonardo"]],
            allow_delegation=False,
            llm=LLMS["cerebras"],
        ),
    }


# ─────────────────────────────────────────────
# AGENTES CUSTOMIZADOS (YAML)
# ─────────────────────────────────────────────
def carregar_customizados() -> list:
    agentes = []
    for arq in Path("agentes_customizados").glob("*.yaml"):
        if arq.name.startswith("EXEMPLO"):
            continue
        try:
            cfg = yaml.safe_load(arq.read_text(encoding="utf-8"))
            ferramentas = [web]
            if cfg.get("pasta_rag") and Path(cfg["pasta_rag"]).exists():
                ferramentas.append(DirectoryReadTool(directory=cfg["pasta_rag"]))
            agentes.append(Agent(
                role=cfg["cargo"],
                goal=cfg["objetivo"],
                backstory=cfg.get("backstory", f"Você é {cfg['nome']}, especialista em {cfg['cargo']}."),
                tools=ferramentas,
                allow_delegation=False,
                llm=LLMS.get(cfg.get("motor", "groq"), LLMS["groq"]),
            ))
            print(f"[KEMY] Agente customizado carregado: {cfg['nome']}")
        except Exception as e:
            print(f"[KEMY][ERRO] Falha ao carregar {arq.name}: {e}")
    return agentes


# ─────────────────────────────────────────────
# PIPELINE DE TASKS (6 tasks encadeadas)
# ─────────────────────────────────────────────
def montar_pipeline(agentes: dict, escopo: str, tem_imagem: bool) -> list:
    task_spec = Task(
        description=(
            f"[MISSÃO KEMY AI]\n{escopo}\n\n"
            "Produza um Documento de Requisitos Técnicos (DRT) com:\n"
            "1. Objetivo do sistema em 1 frase\n"
            "2. Funcionalidades obrigatórias\n"
            "3. Restrições e regras de negócio\n"
            "4. Stack tecnológica recomendada\n"
            "5. Persona do usuário final\n"
            + ("6. Análise visual: cores hex, grids, componentes." if tem_imagem else "")
        ),
        expected_output="DRT estruturado em Markdown.",
        agent=agentes["brenno"],
    )
    task_arq = Task(
        description=(
            "Com base no DRT, defina arquitetura completa:\n"
            "1. Estrutura de componentes e módulos\n"
            "2. Fluxo de dados\n"
            "3. Esquema de banco (se aplicável)\n"
            "4. Rotas de API (método, path, payload)\n"
            "5. Design Patterns aplicados"
        ),
        expected_output="Documento de Arquitetura em Markdown.",
        agent=agentes["diego"],
        context=[task_spec],
    )
    task_front = Task(
        description=(
            "Com base no DRT e Arquitetura, crie a interface:\n"
            "- HTML5 semântico + Tailwind CSS\n"
            "- Mobile-First (md:, lg:, xl:)\n"
            "- Cores exatas do DRT\n"
            "- NUNCA caminhos de imagem locais\n"
            "- Comentários <!-- section: nome --> em cada bloco"
        ),
        expected_output="HTML5 completo com Tailwind inline, começando com <!DOCTYPE html>.",
        agent=agentes["paulo"],
        context=[task_spec, task_arq],
    )
    task_back = Task(
        description=(
            "Implemente ou audite lógica backend:\n"
            "- Rotas FastAPI/Flask ou funções Go\n"
            "- Validação de entrada (Pydantic ou similar)\n"
            "- Queries otimizadas (sem N+1, com índices)\n"
            "- Erros tratados especificamente\n"
            "- Se frontend-only: emita relatório 'N/A - Frontend Only'."
        ),
        expected_output="Código backend comentado OU relatório 'N/A - Frontend Only'.",
        agent=agentes["felipe"],
        context=[task_spec, task_arq],
    )
    task_cyber = Task(
        description=(
            "Audite o código contra OWASP Top 10:\n"
            "1. XSS — inputs sanitizados?\n"
            "2. SQLi — prepared statements?\n"
            "3. Auth — JWT com expiração? bcrypt?\n"
            "4. CORS — origens restritas?\n"
            "5. Dados sensíveis expostos?\n"
            "APROVADO: liste itens verificados.\n"
            "BLOQUEADO: detalhe vulnerabilidade, linha, severidade e correção."
        ),
        expected_output="Relatório: AUDITORIA APROVADA ou BLOQUEADA com detalhes.",
        agent=agentes["bianca"],
        context=[task_front, task_back],
    )
    task_qa = Task(
        description=(
            "Validação final:\n"
            "1. HTML começa com <!DOCTYPE html>?\n"
            "2. Tags fechadas?\n"
            "3. Cores hex batem com o DRT?\n"
            "4. Auditoria da Bianca: APROVADA? Se BLOQUEADA, não entregue.\n"
            "5. Remova qualquer texto fora do código.\n"
            "REGRA: retorne APENAS HTML limpo OU relatório Markdown."
        ),
        expected_output="HTML5 validado começando com <!DOCTYPE html> OU relatório de rejeição.",
        agent=agentes["leonardo"],
        context=[task_front, task_cyber],
    )
    return [task_spec, task_arq, task_front, task_back, task_cyber, task_qa]


# ─────────────────────────────────────────────
# VALIDADOR HTML
# ─────────────────────────────────────────────
def extrair_html(texto: str) -> str:
    match = re.search(r"`{3}(?:html)?\s*([\s\S]*?)`{3}", texto, re.IGNORECASE)
    candidato = match.group(1).strip() if match else texto
    if "<!DOCTYPE html>" in texto or "<html" in texto:
        idx = texto.find("<!DOCTYPE html>")
        idx = idx if idx != -1 else texto.find("<html")
        candidato = texto[idx:].strip()
    if not any(t in candidato.lower() for t in ["<html", "<!doctype"]):
        return ""
    if "<body" not in candidato.lower():
        return ""
    return candidato


# ─────────────────────────────────────────────
# VISÃO COMPUTACIONAL (Gemini)
# ─────────────────────────────────────────────
def processar_imagem(img_b64: str, msg: str, logger: LoggerKemy) -> str:
    logger.info("VISAO", "Processando imagem com Gemini 2.5 Flash...")
    try:
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        partes = img_b64.split(",")
        if len(partes) < 2:
            raise ValueError("base64 malformado")
        mime = partes[0].split(";")[0].split(":")[1]
        data = base64.b64decode(partes[1])
        prompt = (
            f'Pedido do cliente: "{msg}"\n\n'
            "Analise a imagem com precisão técnica:\n"
            "1. Se o cliente quer CRIAR esta UI: descreva cores hex, tipografia, grids, botões, textos.\n"
            "2. Se quer MODIFICAR apenas um elemento: descreva só esse elemento.\n"
            "3. Liste cores dominantes em hex.\n"
            "Responda APENAS com laudo técnico, sem introduções."
        )
        r = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, types.Part.from_bytes(data=data, mime_type=mime)],
        )
        logger.info("VISAO", "Laudo gerado.", {"chars": len(r.text)})
        return f"\n\n[LAUDO VISUAL KEMY AI]:\n{r.text}"
    except ValueError as e:
        logger.erro("VISAO", f"Imagem malformada: {e}")
        return "\n[Aviso: Imagem em formato inválido.]"
    except Exception as e:
        logger.erro("VISAO", f"Falha: {e}")
        return "\n[Aviso: Processamento visual falhou. Continuando sem laudo.]"


# ─────────────────────────────────────────────
# FAILOVER GRANULAR
# ─────────────────────────────────────────────
def executar(crew: Crew, logger: LoggerKemy):
    try:
        for m in ["groq", "gemini", "cerebras"]:
            roteador.usar(m)
        return crew.kickoff()
    except Exception as e:
        logger.alerta("FAILOVER", f"{type(e).__name__}: {e} — Migrando para OpenRouter...")
        agentes_r = [
            Agent(
                role=a.role, goal=a.goal, backstory=a.backstory,
                tools=a.tools or [], allow_delegation=a.allow_delegation,
                llm=LLMS["openrouter"],
            )
            for a in crew.agents
        ]
        gerente_r = Agent(
            role=crew.manager_agent.role,
            goal=crew.manager_agent.goal,
            backstory=crew.manager_agent.backstory,
            allow_delegation=True,
            llm=LLMS["openrouter"],
        )
        crew2 = Crew(
            agents=agentes_r, tasks=crew.tasks,
            process=Process.hierarchical, manager_agent=gerente_r, verbose=True,
        )
        roteador.usar("openrouter")
        return crew2.kickoff()


# ─────────────────────────────────────────────
# FASTAPI — APP
# ─────────────────────────────────────────────
app = FastAPI(
    title="Kemy AI",
    description="Agência Multi-Agente de Engenharia de Software Autônoma",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ORIGENS_CORS", "*").split(","),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


# ─── Modelos ──────────────────────────────────
class Comando(BaseModel):
    mensagem: str = Field(..., min_length=3)
    imagem_base64: Optional[str] = None
    session_id: Optional[str] = None


class NovoAgente(BaseModel):
    nome: str
    cargo: str
    objetivo: str
    backstory: Optional[str] = None
    motor: str = "groq"
    pasta_rag: Optional[str] = None


# ─── Endpoints ────────────────────────────────

@app.get("/")
async def root():
    return {"nome": "Kemy AI", "versao": "2.0.0", "status": "online", "docs": "/docs"}


@app.post("/api/sessao/nova")
async def nova_sessao():
    sid = sessoes.criar()
    return {"session_id": sid, "mensagem": "Kemy AI pronta. Qual é a missão?"}


@app.get("/api/sessao/listar")
async def listar_sessoes():
    return {"sessoes": sessoes.listar()}


@app.post("/api/sessao/{sid}/limpar")
async def limpar_sessao(sid: str):
    if not sessoes.get(sid):
        raise HTTPException(404, "Sessão não encontrada.")
    sessoes.limpar(sid)
    return {"status": "ok", "mensagem": "Memória da sessão limpa."}


@app.get("/api/sessao/{sid}/historico")
async def historico(sid: str):
    s = sessoes.get(sid)
    if not s:
        raise HTTPException(404, "Sessão não encontrada.")
    return {"session_id": sid, "historico": s["historico"]}


@app.post("/api/comando")
async def comando(cmd: Comando):
    # Sessão
    sid = cmd.session_id or sessoes.criar()
    if not sessoes.get(sid):
        sessoes._s[sid] = {
            "codigo_atual": "", "historico": [],
            "criada_em": datetime.utcnow().isoformat(),
            "ultima_atividade": datetime.utcnow().isoformat(),
        }

    logger = LoggerKemy(sid)
    logger.info("KEMY", "Missão recebida.", {"msg": cmd.mensagem[:100]})

    s = sessoes.get(sid)
    ctx_memoria = (
        f"\n[WORKSPACE ATUAL]\n```html\n{s['codigo_atual']}\n```\n"
        "Modifique conforme o pedido, mantendo o que não foi solicitado alterar."
        if s["codigo_atual"] else ""
    )

    laudo = processar_imagem(cmd.imagem_base64, cmd.mensagem, logger) if cmd.imagem_base64 else ""

    escopo = (
        f'PEDIDO: "{cmd.mensagem}"\n{ctx_memoria}{laudo}\n\n'
        "Gabriel Junior, planeje e delegue. Entregue HTML completo ou relatório técnico."
    )

    agentes = montar_agentes()
    customizados = carregar_customizados()
    tasks = montar_pipeline(agentes, escopo, tem_imagem=bool(cmd.imagem_base64))
    todos = list(agentes.values())[1:] + customizados  # sem o gerente (ele é manager)

    crew = Crew(
        agents=todos, tasks=tasks,
        process=Process.hierarchical,
        manager_agent=agentes["gerente"],
        verbose=True,
    )

    try:
        t0 = time.time()
        resultado = executar(crew, logger)
        tempo_total = time.time() - t0
        logger.info("KEMY", f"Missão concluída em {round(tempo_total,2)}s.")
        
        # Registrar no analytics
        analytics_engine.registrar_missao(
            sessao_id=sid,
            agente="crew_geral",
            tipo="missao_completa",
            tempo_segundos=tempo_total,
            tokens_entrada=int(cmd.mensagem.split().__len__() * 4),  # estimativa
            tokens_saida=int(str(resultado).split().__len__() * 4),  # estimativa
            sucesso=True,
        )
    except Exception as e:
        logger.erro("KEMY", f"Falha crítica: {e}")
        analytics_engine.registrar_missao(
            sessao_id=sid,
            agente="crew_geral",
            tipo="missao_completa",
            tempo_segundos=time.time() - t0,
            sucesso=False,
            erro=str(e),
        )
        raise HTTPException(503, f"Todos os motores falharam: {e}")

    texto = str(resultado)
    html = extrair_html(texto)

    if html:
        sessoes.atualizar_codigo(sid, html)
        # Salvar no cache por 60 minutos
        cache_manager.set(sid, cmd.mensagem[:50], html, ttl_minutos=60)
        logger.info("KEMY", "HTML validado e salvo na sessão + cache.")
    else:
        logger.alerta("KEMY", "Nenhum HTML válido no resultado.")

    sessoes.add_historico(sid, cmd.mensagem, texto[:500])

    return {
        "status": "sucesso",
        "session_id": sid,
        "resposta": texto,
        "codigo_gerado": html or texto,
        "html_valido": bool(html),
    }


@app.post("/api/agente/criar")
async def criar_agente(novo: NovoAgente):
    if novo.motor not in ["gemini", "groq", "cerebras", "openrouter"]:
        raise HTTPException(400, "Motor inválido. Use: gemini | groq | cerebras | openrouter")
    slug = re.sub(r"[^a-z0-9_]", "_", novo.nome.lower())
    caminho = Path(f"agentes_customizados/{slug}.yaml")
    if caminho.exists():
        raise HTTPException(409, f"Agente '{novo.nome}' já existe.")
    cfg = {
        "nome": novo.nome, "cargo": novo.cargo, "objetivo": novo.objetivo,
        "backstory": novo.backstory or f"Você é {novo.nome}, especialista em {novo.cargo}.",
        "motor": novo.motor, "pasta_rag": novo.pasta_rag,
        "criado_em": datetime.utcnow().isoformat(),
    }
    caminho.write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    return {"status": "criado", "arquivo": str(caminho), "agente": cfg}


@app.get("/api/agente/listar")
async def listar_agentes():
    custom = []
    for arq in Path("agentes_customizados").glob("*.yaml"):
        if arq.name.startswith("EXEMPLO"):
            continue
        try:
            custom.append(yaml.safe_load(arq.read_text(encoding="utf-8")))
        except Exception as e:
            custom.append({"arquivo": arq.name, "erro": str(e)})
    return {"agentes_fixos": 7, "agentes_customizados": custom}


@app.delete("/api/agente/{slug}")
async def remover_agente(slug: str):
    caminho = Path(f"agentes_customizados/{slug}.yaml")
    if not caminho.exists():
        raise HTTPException(404, "Agente não encontrado.")
    caminho.unlink()
    return {"status": "removido", "slug": slug}


@app.get("/api/logs/{sid}")
async def logs(sid: str):
    p = Path(f"logs/{sid}.jsonl")
    if not p.exists():
        raise HTTPException(404, "Log não encontrado.")
    entradas = []
    for linha in p.read_text(encoding="utf-8").splitlines():
        try:
            entradas.append(json.loads(linha))
        except json.JSONDecodeError:
            pass
    return {"session_id": sid, "total": len(entradas), "logs": entradas}


@app.get("/api/status")
async def status():
    return {
        "nome": "Kemy AI",
        "versao": "2.0.0",
        "status": "online",
        "sessoes_ativas": len(sessoes._s),
        "motores": roteador.status(),
        "cache": cache_manager.status(),
    }


@app.get("/api/analytics/{sid}")
async def analytics_sessao(sid: str):
    """Retorna métricas completas da sessão"""
    if not sessoes.get(sid):
        raise HTTPException(404, "Sessão não encontrada.")
    metricas = analytics_engine.obter_metricas(sid)
    return metricas


@app.get("/api/cache/status")
async def cache_status():
    """Status do cache"""
    return {"cache": cache_manager.status()}


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    print("=" * 55)
    print("  KEMY AI v2.0 — Multi-LLM Agency Engine")
    print(f"  Porta: {port} | Docs: http://localhost:{port}/docs")
    print("=" * 55)
    uvicorn.run("agencia_kemy:app", host="0.0.0.0", port=port, reload=False)
