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
import logging

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.models import Produto, CupomCodigo
from apps.scrapers.progresso import emitir_progresso
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _coletar_cards, _salvar
from apps.scrapers.session_paths import ml_session_dir

caminho_atual = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)

# Marca dos códigos criados por ESTE scraper (vs. curados à mão). Só desativamos/
# reativamos os automáticos; cupons curados manualmente ficam intocados.
_DESC_AUTO = "cupom ML (checkout)"

# Palavras maiúsculas comuns que NÃO são código de cupom (reduz falso-positivo do
# regex sobre texto livre da página).
_NAO_CODIGO = {"OFERTA", "OFERTAS", "VENDIDO", "VENDIDOS", "BREVE", "FRETE", "GRATIS",
               "FULL", "NOVO", "PIX", "OFF", "ATE", "MELI", "SUPER", "MEGA", "TOP",
               "ANDROID", "IPHONE", "SAMSUNG", "MOTOROLA", "XIAOMI", "LG", "SONY",
               "PS4", "PS5", "USB", "HD", "SSD", "GB", "TB", "TV", "LED", "LCD",
               "R$", "COMPRE", "LEVE", "GANHE", "ECONOMIZE"}


def _extrair_codigos(texto):
    """Códigos plausíveis: maiúsculas+dígitos (OFERTAS30, TECH10, FUTEBOL25)."""
    cands = set(re.findall(r"\b[A-Z]{3,}\d{1,3}\b", texto or ""))
    return [c for c in cands if c not in _NAO_CODIGO]


def mapear_cupons_codigo():
    """Raspa /ofertas/cupons: produtos -> origem='cupom_codigo'; códigos -> CupomCodigo."""
    logger.info("Iniciando raspagem de cupons de codigo ML")
    caminho_auth = os.path.join(ml_session_dir(), "auth.json")
    coletados, codigos = [], set()

    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        for n in range(1, 6):  # algumas páginas
            emitir_progresso(f"[PROGRESSO] Cupons-código página {n}/5")
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
                logger.warning("Erro ao carregar pagina %s de cupons de codigo ML: %s", n, e)
                break
            cards = _coletar_cards(page)
            if not cards:
                break
            coletados.extend(cards)
            try:
                codigos.update(_extrair_codigos(page.locator("body").inner_text(timeout=5000)))
            except Exception:
                pass

    # Guarda anti-wipe: se a raspagem não trouxe NADA (ML bloqueou/caiu), não apaga
    # os produtos nem desativa códigos válidos — evita zerar tudo por falha de rede.
    if not coletados and not codigos:
        logger.warning("Raspagem de cupons de codigo ML vazia; nada alterado")
        return 0

    # Produtos do cupom-código são efêmeros (recriados a cada raspagem, como as outras
    # lanes). Só troca se veio conteúdo novo (o guard acima já barra o caso 100% vazio).
    n_prod = 0
    if coletados:
        Produto.objects.filter(
            marketplace="mercadolivre", owner__isnull=True, origem="cupom_codigo"
        ).delete()
        n_prod = _salvar(coletados, origem="cupom_codigo")

    # Códigos: upsert + REATIVA os vistos agora; DESATIVA os automáticos que sumiram
    # (antes só criava e nunca marcava stale → códigos mortos ficavam na lista global).
    n_novos = 0
    for cod in codigos:
        _, criado = CupomCodigo.objects.update_or_create(
            codigo=cod, defaults={"ativo": True})
        # descricao só na criação (não sobrescreve edição manual)
        if criado:
            CupomCodigo.objects.filter(codigo=cod).update(descricao=_DESC_AUTO)
            n_novos += 1

    stale = (CupomCodigo.objects
             .filter(descricao=_DESC_AUTO, ativo=True)
             .exclude(codigo__in=codigos))
    n_stale = stale.update(ativo=False)

    logger.info(
        "Cupons de codigo ML: %s produtos, %s codigos (%s novos, %s desativados)",
        n_prod, len(codigos), n_novos, n_stale,
    )
    return n_prod
