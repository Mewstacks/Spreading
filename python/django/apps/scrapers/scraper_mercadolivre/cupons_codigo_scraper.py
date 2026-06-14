"""
Scraper da página oficial de cupons de CÓDIGO do ML: /ofertas/cupons.

Duas saídas:
  1) Produtos com desconto (de/por) -> Produto(origem='cupom_codigo'). O desconto
     é confirmado na PDP no momento do envio (confiar_desconto=False).
  2) Códigos digitáveis no checkout (ex: OFERTAS30, TECH10) extraídos da página ->
     upsert em CupomCodigo (lista global anexada às mensagens).

Obs: a página não mapeia qual código vale pra cada produto (os códigos são banners
de categoria/site). Por isso o código fica na lista global, não por item.
"""
import os
import re

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.models import Produto, CupomCodigo
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _coletar_cards, _salvar

caminho_atual = os.path.dirname(os.path.abspath(__file__))

# Palavras maiúsculas comuns que NÃO são código de cupom
_NAO_CODIGO = {"OFERTA", "OFERTAS", "VENDIDO", "VENDIDOS", "BREVE", "FRETE", "GRATIS",
               "FULL", "NOVO", "PIX", "OFF", "ATE", "MELI"}


def _extrair_codigos(texto):
    """Códigos plausíveis: maiúsculas+dígitos (OFERTAS30, TECH10, FUTEBOL25)."""
    cands = set(re.findall(r"\b[A-Z]{3,}\d{1,3}\b", texto or ""))
    return [c for c in cands if c not in _NAO_CODIGO]


def mapear_cupons_codigo():
    """Raspa /ofertas/cupons: produtos -> origem='cupom_codigo'; códigos -> CupomCodigo."""
    print("Iniciando raspagem de CUPONS DE CÓDIGO (/ofertas/cupons)...")
    caminho_auth = os.path.join(caminho_atual, "auth.json")
    coletados, codigos = [], set()

    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        for n in range(1, 6):  # algumas páginas
            print(f"[PROGRESSO] Cupons-código página {n}/5")
            url = "https://www.mercadolivre.com.br/ofertas/cupons"
            if n > 1:
                url += f"?page={n}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
            except Exception as e:
                print(f"  Erro página {n}: {e}")
                break
            cards = _coletar_cards(page)
            if not cards:
                break
            coletados.extend(cards)
            try:
                codigos.update(_extrair_codigos(page.locator("body").inner_text(timeout=5000)))
            except Exception:
                pass

    Produto.objects.filter(origem="cupom_codigo").delete()
    n_prod = _salvar(coletados, origem="cupom_codigo")

    n_cod = 0
    for cod in codigos:
        _, criado = CupomCodigo.objects.get_or_create(
            codigo=cod, defaults={"descricao": "cupom ML (checkout)", "ativo": True})
        n_cod += 1 if criado else 0

    print(f"CUPONS-CÓDIGO: {n_prod} produtos, {len(codigos)} códigos ({n_cod} novos): {sorted(codigos)}")
    return n_prod