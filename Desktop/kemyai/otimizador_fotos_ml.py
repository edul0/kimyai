"""
otimizador_fotos_ml.py — Trakyon.ia v2.0
Ativa a opção "Melhorar resolução" (IA do ML) em todas as fotos dos seus anúncios.

PRÉ-REQUISITO: Rodar extrator_anuncios_ml.py primeiro para gerar links_anuncios.txt

USO PRÓPRIO: Automatiza ações na SUA conta. Respeite os Termos de Uso do ML.

COMO USAR:
  1. Abra o Chrome com: chrome.exe --remote-debugging-port=9222
  2. Faça login no Mercado Livre normalmente
  3. Execute: python otimizador_fotos_ml.py
"""

import time
import json
import logging
from pathlib import Path
from playwright.sync_api import sync_playwright, Page, TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────
CHROME_DEBUG_URL = "http://127.0.0.1:9222"
ARQUIVO_LINKS = "links_anuncios.txt"
ARQUIVO_RELATORIO = "relatorio_otimizacao.json"
PAUSA_CARREGAMENTO = 6.0     # segundos para a página carregar
PAUSA_ENTRE_FOTOS = 3.5
PAUSA_PROCESSAMENTO_IA = 4.0  # tempo para a IA do ML processar a foto


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("otimizacao.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("otimizador_fotos")


# ─────────────────────────────────────────────
# RELATÓRIO DE RESULTADOS
# ─────────────────────────────────────────────
class RelatorioOtimizacao:
    def __init__(self):
        self.resultados = {
            "inicio": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_links": 0,
            "fotos_ativadas": 0,
            "fotos_ja_ativas": 0,
            "fotos_sem_ia": 0,
            "erros": 0,
            "detalhes": [],
        }

    def registrar(self, link: str, foto_idx: int, status: str):
        self.resultados["detalhes"].append({
            "link": link[:80],
            "foto": foto_idx + 1,
            "status": status,
        })
        if status == "ativado":
            self.resultados["fotos_ativadas"] += 1
        elif status == "ja_ativo":
            self.resultados["fotos_ja_ativas"] += 1
        elif status == "sem_ia":
            self.resultados["fotos_sem_ia"] += 1
        elif status == "erro":
            self.resultados["erros"] += 1

    def salvar(self):
        self.resultados["fim"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(ARQUIVO_RELATORIO, "w", encoding="utf-8") as f:
            json.dump(self.resultados, f, ensure_ascii=False, indent=2)
        log.info(f"Relatório salvo em '{ARQUIVO_RELATORIO}'.")

    def exibir_resumo(self):
        r = self.resultados
        log.info("\n" + "=" * 60)
        log.info("RESUMO DA OTIMIZAÇÃO")
        log.info(f"  Total de links processados : {r['total_links']}")
        log.info(f"  Fotos com IA ativada agora : {r['fotos_ativadas']}")
        log.info(f"  Fotos já estavam ativas    : {r['fotos_ja_ativas']}")
        log.info(f"  Fotos sem opção de IA      : {r['fotos_sem_ia']}")
        log.info(f"  Erros                      : {r['erros']}")
        log.info("=" * 60)


# ─────────────────────────────────────────────
# DETECÇÃO DE MINIATURAS (MAIS ROBUSTA)
# ─────────────────────────────────────────────
JS_CONTAR_MINIATURAS = """
() => {
    const imgs = document.querySelectorAll('img');
    let count = 0;
    for (const img of imgs) {
        const rect = img.getBoundingClientRect();
        if (rect.width > 10 && rect.width < 150 && rect.left < window.innerWidth / 3 && rect.height > 10) {
            count++;
        }
    }
    return count;
}
"""

JS_CLICAR_MINIATURA = """
(indice) => {
    const imgs = document.querySelectorAll('img');
    const miniaturas = [];
    for (const img of imgs) {
        const rect = img.getBoundingClientRect();
        if (rect.width > 10 && rect.width < 150 && rect.left < window.innerWidth / 3 && rect.height > 10) {
            miniaturas.push(img);
        }
    }
    if (miniaturas[indice]) {
        const container = miniaturas[indice].closest('button')
            || miniaturas[indice].closest('a')
            || miniaturas[indice].parentElement;
        if (container) container.click();
        return true;
    }
    return false;
}
"""


# ─────────────────────────────────────────────
# ATIVADOR DE IA POR FOTO
# ─────────────────────────────────────────────
def ativar_ia_na_foto(page: Page, indice_foto: int, relatorio: RelatorioOtimizacao, link: str):
    """Foca na foto pelo índice e tenta ativar 'Melhorar resolução'."""

    sucesso_clique = page.evaluate(JS_CLICAR_MINIATURA, indice_foto)
    if not sucesso_clique:
        log.warning(f"         [!] Não foi possível clicar na miniatura {indice_foto + 1}.")
        relatorio.registrar(link, indice_foto, "erro")
        return

    page.wait_for_timeout(int(PAUSA_ENTRE_FOTOS * 1000))

    status = "sem_ia"

    try:
        # Estratégia 1: localiza o switch/checkbox diretamente
        chavinha = page.locator('button[role="switch"], input[type="checkbox"]').last
        texto_ia = page.locator("text='Melhorar resolução'").last

        if chavinha.is_visible(timeout=2000):
            is_ativo = (
                chavinha.get_attribute("aria-checked") == "true"
                or chavinha.is_checked()
            )

            if is_ativo:
                log.info("         [~] IA já estava ativa. Pulando.")
                status = "ja_ativo"
            else:
                chavinha.click()
                status = "ativado"

        elif texto_ia.is_visible(timeout=2000):
            # Fallback: clica no texto caso o switch não seja encontrado
            texto_ia.click()
            status = "ativado"
            log.info("         [*] IA ativada via clique no texto (fallback).")

    except PlaywrightTimeout:
        pass  # Mantém status = "sem_ia"
    except Exception as e:
        log.warning(f"         [!] Erro ao interagir com a chave de IA: {type(e).__name__}: {e}")
        status = "erro"

    relatorio.registrar(link, indice_foto, status)

    if status == "ativado":
        log.info("         [*] 'Melhorar resolução' ativada! Aguardando processamento...")
        page.wait_for_timeout(int(PAUSA_PROCESSAMENTO_IA * 1000))

        try:
            btn_salvar = page.locator("button:has-text('Salvar')").first
            if btn_salvar.is_enabled(timeout=3000):
                btn_salvar.click()
                log.info("         [+] Foto salva com sucesso!")
                page.wait_for_load_state("networkidle", timeout=25000)
                page.wait_for_timeout(int(PAUSA_CARREGAMENTO * 1000))
            else:
                log.warning("         [-] Botão 'Salvar' desabilitado. A IA pode não ter gerado resultado.")
        except PlaywrightTimeout:
            log.warning("         [-] Timeout ao tentar salvar. Continuando para próxima foto.")
        except Exception as e:
            log.warning(f"         [!] Erro ao salvar: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────
# PROCESSADOR DE LINK
# ─────────────────────────────────────────────
def processar_link(page: Page, link: str, relatorio: RelatorioOtimizacao):
    """Acessa o editor de fotos de um anúncio e processa todas as fotos."""
    log.info(f"    Acessando editor: {link[:80]}...")

    try:
        page.goto(link, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(int(PAUSA_CARREGAMENTO * 1000))

        qtd_fotos = page.evaluate(JS_CONTAR_MINIATURAS)

        if qtd_fotos == 0:
            log.info("    [!] Nenhuma miniatura encontrada. Pulando.")
            return

        log.info(f"    [*] {qtd_fotos} foto(s) encontrada(s).")

        for indice in range(qtd_fotos):
            log.info(f"      -> Foto {indice + 1} de {qtd_fotos}...")
            ativar_ia_na_foto(page, indice, relatorio, link)

    except PlaywrightTimeout:
        log.warning(f"    [!] Timeout ao acessar editor: {link[:60]}")
        relatorio.resultados["erros"] += 1
    except Exception as e:
        log.error(f"    [!!!] Erro crítico: {type(e).__name__}: {e}")
        relatorio.resultados["erros"] += 1


# ─────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────
def otimizar_fotos_ml():
    log.info("=" * 60)
    log.info("Otimizador de Fotos ML — Trakyon.ia v2.0")
    log.info("=" * 60)

    if not Path(ARQUIVO_LINKS).exists():
        log.error(
            f"Arquivo '{ARQUIVO_LINKS}' não encontrado.\n"
            f"Execute primeiro: python extrator_anuncios_ml.py"
        )
        return

    with open(ARQUIVO_LINKS, "r", encoding="utf-8") as f:
        links = [linha.strip() for linha in f if linha.strip()]

    if not links:
        log.error(f"O arquivo '{ARQUIVO_LINKS}' está vazio.")
        return

    log.info(f"{len(links)} link(s) carregado(s).\n")

    relatorio = RelatorioOtimizacao()
    relatorio.resultados["total_links"] = len(links)

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        except Exception as e:
            log.error(
                f"Não foi possível conectar ao Chrome em {CHROME_DEBUG_URL}.\n"
                f"Inicie o Chrome com: chrome.exe --remote-debugging-port=9222\n"
                f"Erro: {e}"
            )
            return

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        log.info("Conectado ao Chrome com sucesso!\n")
        log.info("=" * 60)

        try:
            for i, link in enumerate(links):
                log.info(f"\n[LINK {i+1}/{len(links)}]")
                processar_link(page, link, relatorio)
        except KeyboardInterrupt:
            log.info("\nOtimização interrompida pelo usuário.")
        finally:
            relatorio.salvar()
            relatorio.exibir_resumo()


if __name__ == "__main__":
    otimizar_fotos_ml()
