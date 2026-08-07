"""Categoria que o próprio Mercado Livre já atribuiu a cada anúncio da página.

O ML não imprime a categoria no card, mas embute o contexto de renderização num
script (``#__NORDIC_RENDERING_CTX__``) onde cada anúncio carrega seu ``domain_id``
(``MLB-VACUUM_CLEANERS``). É o mesmo sinal que `cateorize.popular_macro_categorias`
já consome pelo prefixo, e é muito mais confiável que adivinhar pelo título.

Por que este módulo existe em vez do bloco inline que havia em `scraper.py`:

1. A leitura vivia dentro de um ``except Exception: pass``. Os quatro modos de
   falha (script ausente, marcador renomeado, JSON inválido, timeout do
   Playwright) terminavam no mesmo lugar — catálogo inteiro em 'DESCONHECIDO' —
   e nenhum deixava rastro. Não havia como saber qual consertar.
2. O caminho de ofertas (`ofertas_scraper`, a fonte `mercadolivre-web`, de longe a
   maior do catálogo) nunca teve leitura nenhuma: gravava a constante
   'DESCONHECIDO' direto. Só o caminho de cupom extraía.

Nada aqui levanta: a categoria é enriquecimento e não pode derrubar a coleta de
preço, que é o que a tela realmente precisa. Mas nada aqui falha calado.
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

SELETOR_PAYLOAD = "#__NORDIC_RENDERING_CTX__"
MARCADOR_INICIO = "_n.ctx.r="
MARCADOR_FIM = ";_n.ctx.r.assets"

_MLB = re.compile(r"MLB[A-Z]?\d+")


def id_do_anuncio(link) -> str:
    """MLB id contido na URL do anúncio; '' quando não há nenhum.

    Serve para casar o card com o mapa de `mapear_domain_ids`. O link já chega
    normalizado por `_normalizar_link_produto`, que preserva o item MLB.
    """
    achado = _MLB.search(str(link or ""))
    return achado.group(0) if achado else ""


def _coletar(obj, item_para_cat, produto_para_item):
    """Varre o payload atrás de (anúncio -> domain_id) e dos apelidos de catálogo.

    O mesmo anúncio aparece ora pelo id próprio, ora por `product_id`/
    `catalog_product_id` apontando para o `item_id` real — daí os dois mapas.
    """
    if isinstance(obj, dict):
        domain_id = obj.get("domain_id", "")
        if domain_id:
            item_id = obj.get("id")
            if item_id and _MLB.match(str(item_id)):
                item_para_cat[str(item_id)] = str(domain_id).replace("MLB-", "")

        item_id_ref = obj.get("item_id")
        for chave in ("product_id", "catalog_product_id"):
            apelido = obj.get(chave)
            if apelido and item_id_ref and _MLB.match(str(apelido)):
                produto_para_item[str(apelido)] = str(item_id_ref)

        for valor in obj.values():
            _coletar(valor, item_para_cat, produto_para_item)
    elif isinstance(obj, list):
        for item in obj:
            _coletar(item, item_para_cat, produto_para_item)


def mapear_domain_ids(page) -> dict:
    """``{MLB id: domain_id}`` da página atual. Dict vazio quando o ML não expõe.

    Cada saída vazia é registrada com o motivo: sem isso, uma mudança de layout do
    ML se manifesta apenas como um catálogo silenciosamente sem categoria, meses
    depois, sem nada nos logs apontando para cá.
    """
    try:
        tag = page.locator(SELETOR_PAYLOAD)
        if tag.count() == 0:
            logger.warning(
                "Categorias do ML: script %s ausente na página; os itens desta "
                "coleta ficam sem categoria.", SELETOR_PAYLOAD,
            )
            return {}
        texto = tag.text_content() or ""
    except Exception as erro:
        logger.warning("Categorias do ML: não foi possível ler %s (%s: %s).",
                       SELETOR_PAYLOAD, type(erro).__name__, erro)
        return {}

    if MARCADOR_INICIO not in texto or MARCADOR_FIM not in texto:
        logger.warning(
            "Categorias do ML: marcadores %r/%r não encontrados em %s — o formato "
            "do payload provavelmente mudou.",
            MARCADOR_INICIO, MARCADOR_FIM, SELETOR_PAYLOAD,
        )
        return {}

    try:
        bruto = texto.split(MARCADOR_INICIO, 1)[1].split(MARCADOR_FIM, 1)[0]
        dados = json.loads(bruto)
    except (IndexError, ValueError) as erro:
        logger.warning("Categorias do ML: payload de %s ilegível (%s: %s).",
                       SELETOR_PAYLOAD, type(erro).__name__, erro)
        return {}

    item_para_cat, produto_para_item = {}, {}
    try:
        _coletar(dados, item_para_cat, produto_para_item)
    except RecursionError:
        # Payload muito aninhado: o que já foi coletado continua valendo.
        logger.warning("Categorias do ML: payload fundo demais; mapa parcial "
                       "com %s item(ns).", len(item_para_cat))

    mapa = dict(item_para_cat)
    for produto_id, item_id in produto_para_item.items():
        if item_id in item_para_cat:
            mapa[produto_id] = item_para_cat[item_id]

    if not mapa:
        logger.warning(
            "Categorias do ML: payload lido, mas nenhum domain_id reconhecido — "
            "os itens desta coleta ficam sem categoria.")
    else:
        logger.debug("Categorias do ML: %s anúncio(s) mapeado(s).", len(mapa))
    return mapa
