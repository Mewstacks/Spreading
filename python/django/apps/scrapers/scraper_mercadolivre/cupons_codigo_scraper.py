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
from apps.scrapers.models import Produto, CupomCodigo, FonteIngestao, CupomNormalizado
from apps.scrapers.progresso import emitir_fase, emitir_progresso
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _coletar_cards, _salvar
from apps.scrapers.session_paths import ml_auth_path

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


def mapear_cupons_codigo(faixa=None):
    """Raspa /ofertas/cupons: produtos -> origem='cupom_codigo'; códigos -> CupomCodigo.

    `faixa` (ini, fim) liga o progresso na tela; sem ela a linha sai sem % (é o que
    o ciclo automático faz — não há barra para alimentar).
    """
    logger.info("Iniciando raspagem de cupons de codigo ML")
    caminho_auth = ml_auth_path()
    coletados, codigos = [], set()
    paginas_sem_codigo = 0

    with iniciar_browser(auth_path=caminho_auth, headless=True,
                         validar_sessao=False) as (page, context):
        for n in range(1, 6):  # algumas páginas
            emitir_fase(f"Cupons de checkout — página {n}/5", n / 5, faixa)
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
                achados = _extrair_codigos(page.locator("body").inner_text(timeout=5000))
                codigos.update(achados)
                if not achados:
                    paginas_sem_codigo += 1
            except Exception as e:
                # Era `except Exception: pass`. Com o inner_text estourando o
                # resultado era zero códigos, zero log — e como `coletados` não
                # estava vazio, a raspagem ainda reportava sucesso. O scraper dizia
                # "ok" sem ter trazido um único cupom.
                paginas_sem_codigo += 1
                logger.warning("Não foi possível ler os códigos da página %s de "
                               "cupons ML: %s", n, e)

    # Guarda anti-wipe: se a raspagem não trouxe NADA (ML bloqueou/caiu), não apaga
    # os produtos nem desativa códigos válidos — evita zerar tudo por falha de rede.
    if not coletados and not codigos:
        logger.warning("Raspagem de cupons de codigo ML vazia; nada alterado")
        # Sem prefixo [PROGRESSO]: isto é log, tem que ficar na tela depois que a
        # barra passar, não virar legenda efêmera da barra.
        emitir_progresso("Aviso: a página de cupons do ML não devolveu nenhum item "
                         "(nada foi alterado).")
        return 0

    # Produtos do cupom-código são efêmeros (recriados a cada raspagem, como as outras
    # lanes). Só troca se veio conteúdo novo (o guard acima já barra o caso 100% vazio).
    n_prod = 0
    if coletados:
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
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
        CupomNormalizado.objects.update_or_create(
            fonte=fonte, external_id=f"checkout:{cod}",
            defaults={"marketplace": "mercadolivre", "titulo": f"Cupom {cod}",
                      "codigo": cod, "link": "https://www.mercadolivre.com.br/ofertas/cupons",
                      "confianca": "baixa", "estado": "ativo",
                      "evidencia": {"transport": "public-web", "association": "unverified"}},
        )

    # Uma página pode continuar trazendo produtos e ocultar os banners/códigos por
    # experimento ou localização. Zero códigos não é evidência de expiração.
    n_stale = 0
    if codigos:
        stale = (CupomCodigo.objects
                 .filter(descricao=_DESC_AUTO, ativo=True)
                 .exclude(codigo__in=codigos))
        n_stale = stale.update(ativo=False)

    logger.info(
        "Cupons de codigo ML: %s produtos, %s codigos (%s novos, %s desativados)",
        n_prod, len(codigos), n_novos, n_stale,
    )
    # Página com produtos mas sem nenhum código é o alerta precoce de que o banner
    # mudou de formato ou o inner_text quebrou. Não é motivo para desativar código
    # (a página pode ocultar banners por experimento/localização — ver acima), mas
    # também não pode seguir sendo silêncio.
    if coletados and not codigos:
        logger.warning("Raspagem de cupons de codigo ML: %s produto(s) e NENHUM "
                       "codigo em %s pagina(s)", len(coletados), paginas_sem_codigo)
        emitir_progresso(
            f"Aviso: {len(coletados)} produto(s) de cupom, mas nenhum código de "
            f"checkout legível em {paginas_sem_codigo} página(s).")
    return n_prod
