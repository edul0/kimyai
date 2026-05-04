"""
╔══════════════════════════════════════════════════════════════╗
║      KEMY AI v2.0 — Cache + Analytics Module                ║
║   Gerencia cache de respostas e telemetria de execução       ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import json
import hashlib
import time
from typing import Optional, Dict, Any
from datetime import datetime, timedelta

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False


class CacheManager:
    """Gerencia cache distribuído com Redis (ou memória local em fallback)"""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.redis_client = None
        self.local_cache: Dict[str, tuple[Any, float]] = {}

        if self.redis_url and REDIS_AVAILABLE:
            try:
                self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
                self.redis_client.ping()
                print("[CACHE] Redis conectado ✓")
            except Exception as e:
                print(f"[CACHE] Redis falhou, usando memória local: {e}")
                self.redis_client = None

    def _gerar_chave(self, sessao_id: str, entrada: str) -> str:
        """Gera chave de cache baseada em hash"""
        conteudo = f"{sessao_id}:{entrada}".encode()
        return f"kemy:cache:{hashlib.md5(conteudo).hexdigest()}"

    def set(self, sessao_id: str, entrada: str, valor: str, ttl_minutos: int = 60) -> bool:
        """Salva no cache"""
        chave = self._gerar_chave(sessao_id, entrada)
        ttl_segundos = ttl_minutos * 60

        try:
            if self.redis_client:
                self.redis_client.setex(chave, ttl_segundos, valor)
            else:
                self.local_cache[chave] = (valor, time.time() + ttl_segundos)
            return True
        except Exception as e:
            print(f"[CACHE] Erro ao salvar: {e}")
            return False

    def get(self, sessao_id: str, entrada: str) -> Optional[str]:
        """Recupera do cache"""
        chave = self._gerar_chave(sessao_id, entrada)

        try:
            if self.redis_client:
                return self.redis_client.get(chave)
            else:
                if chave in self.local_cache:
                    valor, expiracao = self.local_cache[chave]
                    if time.time() < expiracao:
                        return valor
                    else:
                        del self.local_cache[chave]
                return None
        except Exception as e:
            print(f"[CACHE] Erro ao recuperar: {e}")
            return None

    def invalidar(self, sessao_id: str, entrada: Optional[str] = None) -> bool:
        """Invalida cache de uma sessão ou entrada específica"""
        try:
            if entrada:
                chave = self._gerar_chave(sessao_id, entrada)
                if self.redis_client:
                    self.redis_client.delete(chave)
                else:
                    self.local_cache.pop(chave, None)
            else:
                # Invalida todas as chaves da sessão
                padrao = f"kemy:cache:*"
                if self.redis_client:
                    keys = self.redis_client.keys(padrao)
                    if keys:
                        self.redis_client.delete(*keys)
                else:
                    self.local_cache.clear()
            return True
        except Exception as e:
            print(f"[CACHE] Erro ao invalidar: {e}")
            return False

    def status(self) -> Dict[str, Any]:
        """Status do cache"""
        if self.redis_client:
            try:
                info = self.redis_client.info()
                return {
                    "tipo": "Redis",
                    "memoria_mb": info.get("used_memory_human", "?"),
                    "keys": self.redis_client.dbsize(),
                    "conectado": True,
                }
            except Exception as e:
                return {"tipo": "Redis", "status": "desconectado", "erro": str(e)}
        else:
            return {"tipo": "Memória Local", "entries": len(self.local_cache)}


class AnalyticsEngine:
    """Coleta e analisa métricas de execução"""

    def __init__(self, redis_url: Optional[str] = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL")
        self.redis_client = None
        self.metricas_locais: list = []

        if self.redis_url and REDIS_AVAILABLE:
            try:
                self.redis_client = redis.from_url(self.redis_url, decode_responses=True)
                print("[ANALYTICS] Redis conectado ✓")
            except Exception as e:
                print(f"[ANALYTICS] Redis falhou: {e}")

    def registrar_missao(
        self,
        sessao_id: str,
        agente: str,
        tipo: str,
        tempo_segundos: float,
        tokens_entrada: int = 0,
        tokens_saida: int = 0,
        sucesso: bool = True,
        erro: Optional[str] = None,
    ) -> bool:
        """Registra execução de uma missão"""
        evento = {
            "ts": datetime.utcnow().isoformat(),
            "sessao_id": sessao_id,
            "agente": agente,
            "tipo": tipo,
            "tempo_s": round(tempo_segundos, 2),
            "tokens_entrada": tokens_entrada,
            "tokens_saida": tokens_saida,
            "tokens_total": tokens_entrada + tokens_saida,
            "sucesso": sucesso,
            "erro": erro,
            "custo_estimado_usd": self._estimar_custo(agente, tokens_entrada, tokens_saida),
        }

        try:
            dados_json = json.dumps(evento, ensure_ascii=False)

            if self.redis_client:
                chave = f"kemy:analytics:{sessao_id}"
                self.redis_client.lpush(chave, dados_json)
                self.redis_client.expire(chave, 86400 * 30)  # 30 dias
            else:
                self.metricas_locais.append(evento)

            return True
        except Exception as e:
            print(f"[ANALYTICS] Erro ao registrar: {e}")
            return False

    def _estimar_custo(self, agente: str, tokens_in: int, tokens_out: int) -> float:
        """Estima custo da chamada baseado no modelo"""
        precos = {
            "gemini": {"entrada": 0.0005, "saida": 0.0015},  # por 1000 tokens
            "groq": {"entrada": 0.00005, "saida": 0.00015},  # ultra barato
            "cerebras": {"entrada": 0.0003, "saida": 0.0006},
            "openrouter": {"entrada": 0.0005, "saida": 0.0015},
        }

        tabela = precos.get(agente, precos["openrouter"])
        custo_in = (tokens_in / 1000) * tabela["entrada"]
        custo_out = (tokens_out / 1000) * tabela["saida"]
        return round(custo_in + custo_out, 6)

    def obter_metricas(self, sessao_id: str, ultimas_n: int = 100) -> Dict[str, Any]:
        """Retorna métricas agregadas da sessão"""
        eventos = []

        try:
            if self.redis_client:
                chave = f"kemy:analytics:{sessao_id}"
                dados = self.redis_client.lrange(chave, 0, ultimas_n - 1)
                eventos = [json.loads(d) for d in dados if d]
            else:
                eventos = [e for e in self.metricas_locais if e["sessao_id"] == sessao_id][
                    -ultimas_n:
                ]

            if not eventos:
                return {"sessao_id": sessao_id, "total": 0, "resumo": {}}

            total_tempo = sum(e.get("tempo_s", 0) for e in eventos)
            total_tokens = sum(e.get("tokens_total", 0) for e in eventos)
            total_custo = sum(e.get("custo_estimado_usd", 0) for e in eventos)
            taxa_sucesso = sum(1 for e in eventos if e.get("sucesso")) / len(eventos)

            agentes_uso = {}
            for e in eventos:
                agente = e.get("agente", "desconhecido")
                if agente not in agentes_uso:
                    agentes_uso[agente] = {"chamadas": 0, "tempo_total": 0, "custo": 0}
                agentes_uso[agente]["chamadas"] += 1
                agentes_uso[agente]["tempo_total"] += e.get("tempo_s", 0)
                agentes_uso[agente]["custo"] += e.get("custo_estimado_usd", 0)

            return {
                "sessao_id": sessao_id,
                "total_eventos": len(eventos),
                "resumo": {
                    "tempo_total_s": round(total_tempo, 2),
                    "tokens_total": total_tokens,
                    "custo_total_usd": round(total_custo, 4),
                    "taxa_sucesso_pct": round(taxa_sucesso * 100, 1),
                    "tempo_medio_s": round(total_tempo / len(eventos), 2),
                },
                "por_agente": agentes_uso,
                "ultimos_10_eventos": eventos[:10],
            }
        except Exception as e:
            return {"sessao_id": sessao_id, "erro": str(e)}

    def limpar_metricas_antigas(self, dias: int = 30) -> bool:
        """Remove métricas com mais de N dias (apenas se usando Redis)"""
        try:
            if self.redis_client:
                limite = datetime.utcnow() - timedelta(days=dias)
                padrao = "kemy:analytics:*"
                keys = self.redis_client.keys(padrao)

                removidas = 0
                for chave in keys:
                    items = self.redis_client.lrange(chave, 0, -1)
                    novos_items = []

                    for item in items:
                        try:
                            evento = json.loads(item)
                            ts = datetime.fromisoformat(evento.get("ts", ""))
                            if ts > limite:
                                novos_items.append(item)
                        except:
                            novos_items.append(item)

                    if novos_items != items:
                        self.redis_client.delete(chave)
                        if novos_items:
                            self.redis_client.rpush(chave, *novos_items)
                        removidas += 1

                print(f"[ANALYTICS] Limpeza concluída: {removidas} chaves atualizadas")
                return True
            else:
                # Fallback para memória local
                limite = datetime.utcnow() - timedelta(days=dias)
                antes = len(self.metricas_locais)
                self.metricas_locais = [
                    e
                    for e in self.metricas_locais
                    if datetime.fromisoformat(e.get("ts", "")) > limite
                ]
                print(f"[ANALYTICS] {antes - len(self.metricas_locais)} eventos removidos")
                return True
        except Exception as e:
            print(f"[ANALYTICS] Erro ao limpar: {e}")
            return False


# Instâncias globais
cache_manager = CacheManager()
analytics_engine = AnalyticsEngine()
