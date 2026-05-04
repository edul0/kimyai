"""
╔══════════════════════════════════════════════════════════════╗
║      KEMY AI v2.0 — Implementação de Exemplo Prático         ║
║   Como usar Kemy AI com código real — exemplos de produção   ║
╚══════════════════════════════════════════════════════════════╝
"""

# ─── EXEMPLO 1: Criar Landing Page de SaaS ────────────────────

import requests
import json
import time

KEMY_API = "http://localhost:8000"  # ou sua URL na nuvem

# Step 1: Criar nova sessão
resposta = requests.post(f"{KEMY_API}/api/sessao/nova")
session_id = resposta.json()["session_id"]
print(f"✓ Sessão criada: {session_id}")

# Step 2: Enviar comando com imagem (opcional)
comando = {
    "mensagem": """
    Crie uma landing page moderna para um SaaS de gerenciamento de projetos.
    
    Requisitos:
    - Hero section com CTA "Começar grátis"
    - Seção de features com 6 funcionalidades principais
    - Pricing com 3 planos (Starter, Pro, Enterprise)
    - Testimonials com avatares
    - Footer com links e social media
    - Mobile-first com Tailwind CSS
    - Cores: azul principal (#0066FF), cinza secundário (#F3F4F6)
    """,
    "session_id": session_id,
    # "imagem_base64": "data:image/png;base64,..."  # Opcional
}

resposta = requests.post(
    f"{KEMY_API}/api/comando",
    json=comando,
    headers={"Content-Type": "application/json"}
)

resultado = resposta.json()
print(f"✓ Status: {resultado['status']}")
print(f"✓ HTML Válido: {resultado['html_valido']}")

# Step 3: Salvar o HTML
if resultado["html_valido"]:
    with open("landing_page.html", "w", encoding="utf-8") as f:
        f.write(resultado["codigo_gerado"])
    print("✓ HTML salvo em landing_page.html")

# Step 4: Ver histórico completo
resposta_historico = requests.get(
    f"{KEMY_API}/api/sessao/{session_id}/historico"
)
historico = resposta_historico.json()["historico"]
print(f"✓ Conversas: {len(historico)}")
for i, conversa in enumerate(historico):
    print(f"  {i+1}. Usuário: {conversa['usuario'][:60]}...")
    print(f"     Kemy: {conversa['kemy'][:60]}...")


# ─── EXEMPLO 2: Criar Agente Customizado ──────────────────────

novo_agente = {
    "nome": "Analytics Specialist",
    "cargo": "Especialista em Analytics e Data",
    "objetivo": "Implementar Google Analytics, Mixpanel e dashboards de conversão",
    "backstory": """Você é especialista em Analytics com foco em Product Growth.
    Conhece Google Analytics 4, Mixpanel, Segment, e interpretação de funis.
    Sempre verifica: eventos estão bem rastreados? Funis estão configurados?
    Atributos de usuário cobrem personas?""",
    "motor": "groq",
    "pasta_rag": "./conhecimento/analytics_specialist"
}

resposta = requests.post(
    f"{KEMY_API}/api/agente/criar",
    json=novo_agente
)
print(f"✓ Agente criado: {resposta.json()['agente']['nome']}")


# ─── EXEMPLO 3: Verificar Analytics de Uma Missão ──────────────

resposta = requests.get(f"{KEMY_API}/api/analytics/{session_id}")
analytics = resposta.json()

print(f"\n📊 ANALYTICS DA SESSÃO {session_id}:")
print(f"   Total de eventos: {analytics['total_eventos']}")
print(f"   Resumo:")
print(f"     - Tempo total: {analytics['resumo']['tempo_total_s']}s")
print(f"     - Tokens: {analytics['resumo']['tokens_total']}")
print(f"     - Custo: ${analytics['resumo']['custo_total_usd']:.4f}")
print(f"     - Taxa de sucesso: {analytics['resumo']['taxa_sucesso_pct']}%")

print(f"\n   Por agente:")
for agente, dados in analytics['por_agente'].items():
    print(f"     {agente}:")
    print(f"       - Chamadas: {dados['chamadas']}")
    print(f"       - Tempo: {dados['tempo_total']:.1f}s")
    print(f"       - Custo: ${dados['custo']:.4f}")


# ─── EXEMPLO 4: Obter Logs Estruturados ───────────────────────

resposta = requests.get(f"{KEMY_API}/api/logs/{session_id}")
logs = resposta.json()["logs"]

print(f"\n📝 LOGS DA SESSÃO ({len(logs)} eventos):")
for log in logs[-5:]:  # Últimos 5
    print(f"   [{log['nivel'].upper()}] {log['agente']}: {log['msg']}")
    if log['dados']:
        print(f"      Dados: {json.dumps(log['dados'], ensure_ascii=False)}")


# ─── EXEMPLO 5: Deploy em Render.com ───────────────────────────

"""
PASSO A PASSO PARA DEPLOY:

1. Conecte seu repositório GitHub em render.com:
   https://dashboard.render.com/select-repo

2. Selecione este repositório (kemyai)

3. Configure:
   - Service Type: Web Service
   - Build Command: (deixe em branco — usa Dockerfile)
   - Start Command: (deixe em branco — usa Dockerfile)
   
4. Environment Variables:
   GEMINI_API_KEY=<sua-chave>
   GROQ_API_KEY=<sua-chave>
   CEREBRAS_API_KEY=<sua-chave>
   OPENROUTER_API_KEY=<sua-chave>
   REDIS_URL=<sua-redis-url> (opcional — use Redis Cloud)
   ORIGENS_CORS=https://seu-frontend.com

5. Deploy! 🚀

6. Sua API estará em:
   https://seu-servico.onrender.com
   
   Docs Swagger: https://seu-servico.onrender.com/docs

"""


# ─── EXEMPLO 6: Usar em Produção (com erro handling) ──────────

def criar_landing_com_retry(mensagem: str, max_tentativas: int = 3) -> dict:
    """Cria landing page com retry automático"""
    
    # Criar sessão
    resposta = requests.post(f"{KEMY_API}/api/sessao/nova")
    session_id = resposta.json()["session_id"]
    
    for tentativa in range(max_tentativas):
        try:
            resposta = requests.post(
                f"{KEMY_API}/api/comando",
                json={
                    "mensagem": mensagem,
                    "session_id": session_id
                },
                timeout=180  # 3 minutos
            )
            resposta.raise_for_status()
            
            resultado = resposta.json()
            
            if resultado["html_valido"]:
                return {"sucesso": True, "html": resultado["codigo_gerado"], "session_id": session_id}
            else:
                print(f"⚠️ Tentativa {tentativa+1}: HTML inválido. Retentando...")
                time.sleep(5)
                
        except requests.exceptions.Timeout:
            print(f"⚠️ Tentativa {tentativa+1}: Timeout. Retentando...")
            time.sleep(5)
        except Exception as e:
            print(f"❌ Erro na tentativa {tentativa+1}: {e}")
            if tentativa == max_tentativas - 1:
                return {"sucesso": False, "erro": str(e)}
            time.sleep(10)
    
    return {"sucesso": False, "erro": "Falha após todas as tentativas"}


# Usar:
resultado = criar_landing_com_retry(
    "Crie uma landing page para um app mobile de delivery"
)

if resultado["sucesso"]:
    print("✅ Landing page criada com sucesso!")
    with open("output.html", "w", encoding="utf-8") as f:
        f.write(resultado["html"])
else:
    print(f"❌ Falha: {resultado['erro']}")
