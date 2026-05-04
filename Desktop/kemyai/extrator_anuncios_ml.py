"""
extrator_anuncios_ml.py — Trakyon.ia v2.0
Coleta links do editor de fotos dos seus anúncios no Mercado Livre.

USO PRÓPRIO: Este script automatiza ações na SUA conta do Mercado Livre.
Uso em contas de terceiros sem autorização viola os Termos de Uso do ML.

COMO USAR:
  1. Abra o Chrome com: chrome.exe --remote-debugging-port=9222
  2. Faça login no Mercado Livre normalmente
  3. Execute: python extrator_anuncios_ml.py
"""

import time
import json
import logging
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout


# ─────────────────────────────────────────────
# CONFIGURAÇÃO
# ─────────────────────────────────────────────
CHROME_DEBUG_URL = "http://127.0.0.1:9222"
URL_LISTA_ANUNCIOS = "https://www.mercadolivre.com.br/anuncios/lista?filters=OMNI_ACTIVE"
ARQUIVO_SAIDA = "links_anuncios.txt"
ARQUIVO_PROGRESSO = "progresso_extracao.json"
PAUSA_ENTRE_PAGINAS = 5.0      # segundos
PAUSA_ENTRE_ANUNCIOS = 4.0
PAUSA_APOS_CLIQUE = 4.5


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("extracao.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("extrator_ml")


# ─────────────────────────────────────────────
# GERENCIADOR DE PROGRESSO (RETOMADA APÓS FALHA)
# ─────────────────────────────────────────────
def carregar_progresso() -> dict:
    """Carrega progresso anterior para retomar extração interrompida."""
    if Path(ARQUIVO_PROGRESSO).exists():
        with open(ARQUIVO_PROGRESSO, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"links_processados": [], "pagina_atual": 1, "inicio": datetime.now().isoformat()}


def salvar_progresso(estado: dict):
    with open(ARQUIVO_PROGRESSO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# FUNÇÕES AUXILIARES
# ─────────────────────────────────────────────
def abrir_gaveta_variacoes(aba: Page):
    """Abre a aba 'Variações e fotos' se existir."""
    try:
        aba_var = aba.locator("text=/Variações e fotos/i").first
        if aba_var.is_visible(timeout=2000):
            aba_var.scroll_into_view_if_needed()
            aba_var.click()
            aba.wait_for_timeout(int(2000))
    except PlaywrightTimeout:
        pass  # Aba não existe neste anúncio, prossegue


def e_url_editor(url: str) -> bool:
    """Verifica se a URL é de um editor de fotos do ML."""
    termos = ["photo", "editor", "studio", "foto", "imagen"]
    return any(t in url.lower() for t in termos)


def extrair_links_do_anuncio(
    context: BrowserContext,
    link_anuncio: str,
    progresso: dict,
) -> set:
    """
    Abre um anúncio, clica em cada botão 'editor de fotos' e coleta URLs.
    Retorna conjunto de URLs capturadas.
    """
    if link_anuncio in progresso["links_processados"]:
        log.info(f"[PULANDO] Já processado anteriormente: {link_anuncio[:60]}")
        return set()

    links_capturados = set()
    aba = None

    try:
        aba = context.new_page()

        url_completa = link_anuncio
        if url_completa.startswith("/"):
            url_completa = "https://www.mercadolivre.com.br" + url_completa

        aba.goto(url_completa, wait_until="domcontentloaded", timeout=30000)
        aba.wait_for_timeout(int(PAUSA_ENTRE_ANUNCIOS * 1000))

        abrir_gaveta_variacoes(aba)

        botoes_editor = aba.locator("text=/editor de fotos/i")
        qtd_botoes = botoes_editor.count()

        if qtd_botoes == 0:
            log.info("    [-] Nenhum botão 'editor de fotos' encontrado.")
            return set()

        log.info(f"    [*] {qtd_botoes} botão(ões) de editor encontrado(s).")

        for j in range(qtd_botoes):
            try:
                btn = aba.locator("text=/editor de fotos/i").nth(j)
                abas_antes = len(context.pages)

                btn.evaluate("el => { const a = el.closest('a'); if (a) a.click(); else el.click(); }")
                aba.wait_for_timeout(int(PAUSA_APOS_CLIQUE * 1000))

                abas_depois = len(context.pages)

                if abas_depois > abas_antes:
                    aba_editor = context.pages[-1]
                    aba_editor.wait_for_load_state("domcontentloaded", timeout=15000)
                    url_atual = aba_editor.url

                    if e_url_editor(url_atual):
                        links_capturados.add(url_atual)
                        log.info(f"    [+] Capturado (nova aba): {url_atual[:80]}")

                    aba_editor.close()
                    aba.bring_to_front()
                else:
                    url_atual = aba.url
                    if e_url_editor(url_atual):
                        links_capturados.add(url_atual)
                        log.info(f"    [+] Capturado (mesma aba): {url_atual[:80]}")
                        aba.go_back()
                        aba.wait_for_timeout(int(PAUSA_ENTRE_ANUNCIOS * 1000))
                        abrir_gaveta_variacoes(aba)
                    else:
                        log.info(f"    [-] Clique não redirecionou para editor. URL: {url_atual[:60]}")

            except PlaywrightTimeout:
                log.warning(f"    [!] Timeout no botão {j+1}. Continuando...")
            except Exception as e:
                log.warning(f"    [!] Erro inesperado no botão {j+1}: {type(e).__name__}: {e}")

    except PlaywrightTimeout:
        log.warning(f"    [!] Timeout ao carregar anúncio: {link_anuncio[:60]}")
    except Exception as e:
        log.error(f"    [!!!] Erro crítico no anúncio {link_anuncio[:60]}: {type(e).__name__}: {e}")
    finally:
        if aba and not aba.is_closed():
            aba.close()

    return links_capturados


# ─────────────────────────────────────────────
# FUNÇÃO PRINCIPAL
# ─────────────────────────────────────────────
def extrair_links_ml():
    log.info("=" * 60)
    log.info("Extrator de Anúncios ML — Trakyon.ia v2.0")
    log.info("=" * 60)

    progresso = carregar_progresso()
    links_coletados: set = set()
    pagina_num = progresso.get("pagina_atual", 1)

    log.info(f"Retomando da página {pagina_num}. Links já processados: {len(progresso['links_processados'])}")

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CHROME_DEBUG_URL)
        except Exception as e:
            log.error(
                f"Não foi possível conectar ao Chrome em {CHROME_DEBUG_URL}.\n"
                f"Certifique-se de iniciar o Chrome com:\n"
                f"  chrome.exe --remote-debugging-port=9222\n"
                f"Erro: {e}"
            )
            return

        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        log.info("Conectado ao Chrome com sucesso.")

        try:
            log.info(f"Navegando para a lista de anúncios...")
            page.goto(URL_LISTA_ANUNCIOS, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(int(PAUSA_ENTRE_PAGINAS * 1000))

            while True:
                log.info(f"\n{'='*40}\nAnalisando página {pagina_num}...")

                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)

                elementos = page.locator("a[href*='/modificar/']").all()
                links_edicao = [
                    el.get_attribute("href")
                    for el in elementos
                    if el.get_attribute("href")
                ]

                log.info(f"Encontrados {len(links_edicao)} anúncios nesta página.")

                if not links_edicao:
                    log.info("Nenhum anúncio encontrado. Encerrando.")
                    break

                for i, link in enumerate(links_edicao):
                    log.info(f"\n[ANÚNCIO {i+1}/{len(links_edicao)}] Processando...")
                    novos = extrair_links_do_anuncio(context, link, progresso)
                    links_coletados.update(novos)

                    # Registra progresso para retomada
                    if link not in progresso["links_processados"]:
                        progresso["links_processados"].append(link)
                    salvar_progresso(progresso)

                # Próxima página
                btn_next = page.locator("li.andes-pagination__button--next > a")
                if btn_next.is_visible():
                    log.info(f"\nAvançando para página {pagina_num + 1}...")
                    btn_next.click()
                    page.wait_for_timeout(int(PAUSA_ENTRE_PAGINAS * 1000))
                    pagina_num += 1
                    progresso["pagina_atual"] = pagina_num
                    salvar_progresso(progresso)
                else:
                    log.info("\nFim da lista de anúncios.")
                    break

        except KeyboardInterrupt:
            log.info("\nExtração interrompida pelo usuário. Progresso salvo.")
        except Exception as e:
            log.error(f"Erro inesperado durante a extração: {type(e).__name__}: {e}")
        finally:
            # Salva resultados parciais
            if links_coletados:
                modo = "a" if Path(ARQUIVO_SAIDA).exists() else "w"
                with open(ARQUIVO_SAIDA, modo, encoding="utf-8") as f:
                    for link in sorted(links_coletados):
                        f.write(link + "\n")

    log.info("\n" + "=" * 60)
    log.info(f"EXTRAÇÃO FINALIZADA! {len(links_coletados)} links novos salvos em '{ARQUIVO_SAIDA}'.")
    log.info("=" * 60)


if __name__ == "__main__":
    extrair_links_ml()
