"""
Ingestão de ofertas da Amazon (amazon.com.br) via Creators API.

A Creators API NÃO expõe um feed de "ofertas do dia"; montamos o feed varrendo
buscas (searchItems) de palavras-chave amplas com filtro de desconto mínimo. Cada
item vira um Produto (marketplace='amazon'), mantendo os mesmos nomes de campo do
ML para reaproveitar seleção/envio:
  preco_sem_desconto = preço "de" (savingBasis), preco_com_cupom = preço atual.

Cupons da Amazon BR são em geral PROMOÇÕES de clipar (sem código de checkout). Por
isso origem='cupom_codigo' aqui significa "tem promoção/cupom clicável" — o desconto
entra nos campos de preço + rótulo; codigo_checkout fica vazio (não há código).
"""
from django.conf import settings
import logging
import re

from apps.scrapers.models import Produto
from apps.scrapers.progresso import emitir_progresso
from apps.scrapers.scraper_amazon import creators_api
# Reaproveita a classificação por nome (PT) já usada no ML.
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome

logger = logging.getLogger(__name__)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _primeiro(d, *caminhos):
    """Retorna o 1º caminho aninhado presente. caminho = tupla de chaves."""
    for caminho in caminhos:
        cur = d
        ok = True
        for k in caminho:
            if isinstance(cur, list):
                cur = cur[0] if cur else None
            if not isinstance(cur, dict) or k not in cur:
                ok = False
                break
            cur = cur[k]
        if ok and cur not in (None, ""):
            return cur
    return None


def _reconstruir_preco_de(listing: dict, preco_atual: float) -> float:
    """Preço "De:" derivado de `price.savings` quando `savingBasis` não veio.

    Prefere o valor absoluto (soma exata); só cai na porcentagem se ela for o único
    dado. Devolve `preco_atual` quando não há como reconstruir — o chamador trata
    isso como "sem desconto de/por".
    """
    economia = _num(_primeiro(listing, ("price", "savings", "money", "amount")))
    if economia > 0:
        return preco_atual + economia
    pct = _num(_primeiro(listing, ("price", "savings", "percentage")))
    if 0 < pct < 100:
        return preco_atual / (1 - pct / 100)
    return preco_atual


def _taxonomia(item: dict) -> tuple[str, str]:
    """(categoria, macro) a partir de `browseNodeInfo` — o que a Amazon já classificou.

    Sem isto todo produto Amazon nascia com `categoria='DESCONHECIDO'` e macro
    adivinhada pelo título. Duas consequências reais: o filtro de subcategoria da
    vitrine (que exclui 'DESCONHECIDO') escondia a loja inteira, e títulos longos
    caíam na macro errada — "DREAME F10 Robô Aspirador ... com câmera" batia em
    'Áudio, Vídeo e Fotografia' antes de chegar em 'Eletrodomésticos'.

    O nó da Amazon é um sinal muito mais forte que o título, então ele vem
    primeiro na classificação; o nome continua como fallback.
    """
    nos = _primeiro(item, ("browseNodeInfo", "browseNodes")) or []
    if isinstance(nos, dict):
        nos = [nos]
    nomes = []
    for no in nos:
        if not isinstance(no, dict):
            continue
        if no.get("isRoot"):
            continue  # raiz é "Todos os departamentos": não distingue nada
        rotulo = str(no.get("displayName") or no.get("contextFreeName") or "").strip()
        if rotulo:
            nomes.append(rotulo)
    categoria = nomes[0][:100] if nomes else ""
    macro = classificar_oferta_por_nome(" ".join(nomes)) if nomes else None
    return categoria, macro or ""


def _mapear_item(item: dict) -> dict | None:
    """
    Converte um item bruto da Creators API -> dict no formato do _upsert.
    Isola aqui todo o conhecimento do schema (lowerCamelCase). None se incompleto.
    """
    asin = item.get("asin") or item.get("ASIN")
    if not asin:
        return None

    nome = _primeiro(item, ("itemInfo", "title", "displayValue")) or ""
    if not nome:
        return None

    listing = _primeiro(item, ("offersV2", "listings")) or {}
    if isinstance(listing, list):
        listing = listing[0] if listing else {}

    # Creators API catalog/v1: preço atual e "De:" (savingBasis) vivem DENTRO de price.
    #   price.money.amount            -> preço atual
    #   price.savingBasis.money.amount-> preço "De:" (lista/anterior)
    #   price.savings.percentage      -> % de desconto
    preco_atual = _num(_primeiro(listing, ("price", "money", "amount")))
    preco_de = _num(_primeiro(listing, ("price", "savingBasis", "money", "amount")))
    if preco_atual <= 0:
        return None
    if preco_de <= preco_atual:
        # `savingBasis` é opcional no catalog/v1 e falta com frequência. Como a busca
        # já foi filtrada por `minSavingPercent`, o item TEM desconto — mas sem o "De:"
        # ele saía daqui com preco_de == preco_atual e o feed (que exige de > por) o
        # descartava. Reconstruímos o "De:" a partir do desconto que a própria API
        # informa, em vez de jogar fora uma oferta legítima.
        preco_de = _reconstruir_preco_de(listing, preco_atual)
    if preco_de <= preco_atual:
        preco_de = preco_atual  # sem desconto de/por; pode ainda ter promoção (dealDetails)
    elif preco_de > preco_atual:
        # savingBasis às vezes vem em unidade/escala diferente do price, inflando o
        # "De:" e gerando desconto falso. A própria API informa a porcentagem; quando
        # ela discorda do par (de, atual), o item está corrompido e é descartado —
        # silenciar o "De:" deixaria o produto entrar no catálogo com desconto zero
        # e poluiria o ranking com dado que sabemos estar errado.
        pct_api = _primeiro(listing, ("price", "savings", "percentage"))
        if pct_api is not None:
            pct_calc = (preco_de - preco_atual) / preco_de * 100
            if abs(_num(pct_api) - pct_calc) > 2:
                logger.warning(
                    "amazon_preco_incoerente asin=%s de=%.2f atual=%.2f api=%.2f%% calc=%.2f%%",
                    asin, preco_de, preco_atual, _num(pct_api), pct_calc,
                )
                return None
        elif preco_de >= preco_atual * 10:
            # Sem a porcentagem da API, resta a guarda de escala.
            logger.warning(
                "amazon_preco_incoerente asin=%s de=%.2f atual=%.2f (sem percentage)",
                asin, preco_de, preco_atual,
            )
            return None

    # Promoção/cupom de clipar agora vem em dealDetails (null quando não há).
    deal = _primeiro(listing, ("dealDetails",))
    tem_promocao = bool(deal)
    rotulo_promo = ""
    if isinstance(deal, dict):
        rotulo_promo = (deal.get("displayName") or deal.get("title")
                        or deal.get("badge") or deal.get("type") or "Promoção")[:60]
    # `dealDetails` também representa lightning deals. Só tratamos como cupom
    # quando a fonte afirma explicitamente coupon/cupom; qualquer outra promoção é
    # exibida como desconto de preço, sem instruir o público a "ativar" algo falso.
    cupom_confirmado = bool(re.search(r"coupon|cupom", rotulo_promo, re.I))

    # Prime/deliveryInfo não existe mais no catalog/v1. Aproxima "vendido pela Amazon"
    # (merchantInfo.name) como selo de confiança no lugar do antigo frete_full/Prime.
    merchant = _primeiro(listing, ("merchantInfo", "name")) or ""
    prime = "amazon" in merchant.lower()
    imagem = _primeiro(item, ("images", "primary", "large", "url")) or ""
    link = (item.get("detailPageURL") or item.get("detailPageUrl")
            or f"https://{settings.AMAZON_MARKETPLACE}/dp/{asin}")
    categoria, macro_sugerida = _taxonomia(item)

    return {
        "asin": asin,
        "nome": nome[:255],
        "preco_sem_desconto": preco_de,
        "preco_com_cupom": preco_atual,
        "link_produto": link.split("#")[0],
        "imagem_url": imagem[:1000],
        "frete_full": prime,
        "tem_promocao": tem_promocao,
        "rotulo_promo": rotulo_promo,
        "cupom_confirmado": cupom_confirmado,
        "categoria": categoria,
        "macro_sugerida": macro_sugerida,
    }


def _upsert_produto(m: dict, origem: str, macro=None, owner=None) -> bool:
    """Cria/atualiza Produto por (marketplace, asin, owner). Itens Amazon são PRIVADOS
    do usuário (owner=usuario), pois vêm da conta Creators dele. Preserva cache."""
    Produto.objects.update_or_create(
        marketplace="amazon",
        asin=m["asin"],
        owner=owner,
        defaults={
            "origem": origem,
            "nome": m["nome"],
            "preco_sem_desconto": m["preco_sem_desconto"],
            "preco_com_cupom": m["preco_com_cupom"],
            "preco_fonte": m["preco_sem_desconto"],
            "preco_efetivo": m["preco_com_cupom"],
            "fonte": "amazon-creators-api",
            "estado": "ativo",
            "falha_verificacao": "",
            "link_produto": m["link_produto"],
            # A vitrine oculta 'DESCONHECIDO' do filtro de subcategoria; gravar o nó
            # da Amazon é o que torna a loja filtrável junto com o Mercado Livre.
            "categoria": m.get("categoria") or "DESCONHECIDO",
            "macro_categoria": (macro or m.get("macro_sugerida")
                                or classificar_oferta_por_nome(m["nome"])),
            "imagem_url": m["imagem_url"],
            "frete_full": m["frete_full"],
            "codigo_checkout": "",  # Amazon: cupom é de clipar, não tem código
            "evidencia": {"promotion": {"present": m["tem_promocao"],
                                         "label": m["rotulo_promo"],
                                         "coupon_confirmed": m["cupom_confirmado"]}},
        },
    )
    # Histórico de preços (B1): observação por asin p/ medir queda real depois.
    from apps.scrapers.precos import registrar
    registrar("amazon", m["asin"], m["link_produto"], m["preco_com_cupom"])
    return True


def _paginas_padrao() -> int:
    """Páginas por keyword. A Creators API devolve no máximo 10 itens por página, então
    este número é o teto real do feed: 2 páginas limitavam cada keyword a 20 itens."""
    return max(1, int(getattr(settings, "AMAZON_FEED_PAGES", 5)))


def _coletar(keyword, min_savings, max_paginas=None, creds=None):
    """Coleta itens mapeados de uma busca (até max_paginas) com as creds dadas."""
    out = []
    max_paginas = _paginas_padrao() if max_paginas is None else max_paginas
    for p in range(1, max_paginas + 1):
        itens = creators_api.search_items(
            keyword, min_savings_percent=min_savings, item_count=10, page=p, creds=creds
        )
        if not itens:
            break
        for it in itens:
            m = _mapear_item(it)
            if m:
                out.append(m)
    return out


def _coletar_termos(termos, min_savings, *, creds, max_paginas=None, rotulo=""):
    """Coleta uma lista de buscas. Um termo quebrado não derruba os outros.

    Mas TODOS quebrados não podem virar "zero ofertas" silencioso: era assim que a
    Amazon sumia da vitrine (429 de cota estourada, host errado, credencial trocada)
    sem que nada no painel, no SSE ou no Perfil dissesse por quê. Quando nenhuma
    busca sobrevive, o erro sobe para quem chamou decidir o que mostrar.
    """
    itens, falhas, ultimo_erro = [], 0, None
    termos = list(termos)
    for i, termo in enumerate(termos, 1):
        if rotulo:
            emitir_progresso(f"[PROGRESSO] {rotulo} {i}/{len(termos)}: {termo}")
        try:
            itens.extend(_coletar(termo, min_savings, max_paginas, creds=creds))
        except (creators_api.AmazonNotEligible, creators_api.AmazonConfigError):
            # Permanentes para o ciclo inteiro: repetir a mesma credencial recusada
            # em 12 keywords só multiplica a espera do throttle por 12.
            raise
        except Exception as exc:
            falhas += 1
            ultimo_erro = exc
            logger.warning("Busca Amazon '%s' falhou: %s", termo, exc)
    if termos and falhas == len(termos):
        raise creators_api.AmazonAPIError(
            f"Nenhuma das {falhas} buscas da Amazon respondeu; última falha: {ultimo_erro}"
        )
    return itens


def coletar_feed(usuario=None, creds=None):
    """UMA varredura das keywords do feed, compartilhada por ofertas e promoções.

    `mapear_ofertas` e `mapear_cupons_codigo` pediam exatamente as mesmas páginas à
    Creators API, com as mesmas credenciais e o mesmo `minSavingPercent` — 120
    chamadas onde 60 bastam. Numa conta nova, cuja cota diária é pequena, essa
    duplicação é o que leva ao 429 que derruba a coleta inteira.
    """
    creds = creds if creds is not None else creators_api.creds_de_usuario(usuario)
    min_savings = int(getattr(settings, "AMAZON_MIN_SAVINGS_PCT", 15))
    keywords = getattr(settings, "AMAZON_FEED_KEYWORDS", []) or []
    logger.info("Amazon feed user=%s: %s keywords, min %s%% off",
                getattr(usuario, "id", None), len(keywords), min_savings)
    return _coletar_termos(keywords, min_savings, creds=creds, rotulo="Amazon ofertas")


def mapear_ofertas(usuario=None, itens=None):
    """Keywords de feed com desconto real -> origem='oferta', owner=usuario.
    Itens Amazon são privados do usuário (raspados com a conta Creators dele)."""
    itens = coletar_feed(usuario) if itens is None else itens
    total = 0
    for m in itens:
        # só feed: itens com desconto real de/por
        if m["preco_sem_desconto"] > m["preco_com_cupom"]:
            _upsert_produto(m, origem="oferta", owner=usuario)
            total += 1
    logger.info("Amazon ofertas: %s upserts", total)
    return total


def buscar_por_termo(termo_busca, min_desconto=15, max_paginas=None, macro=None, usuario=None):
    """searchItems por sub-nicho (lista separada por vírgula) -> origem='busca', owner=usuario."""
    creds = creators_api.creds_de_usuario(usuario)
    termos = [t.strip() for t in (termo_busca or "").split(",") if t.strip()]
    min_desconto = int(min_desconto or 0)
    itens = _coletar_termos(termos, min_desconto, creds=creds, max_paginas=max_paginas)
    total = descartados = 0
    for m in itens:
        # `minSavingPercent` é um pedido, não um filtro: a Creators API devolve itens
        # de 0% na mesma resposta. Sem repetir o corte aqui, uma busca "mínimo 15%"
        # gravava produto sem desconto nenhum — que a vitrine depois esconde, então
        # o contador do SSE prometia itens que a tela nunca mostrava.
        if not _tem_desconto_minimo(m, min_desconto):
            descartados += 1
            continue
        _upsert_produto(m, origem="busca", macro=macro, owner=usuario)
        total += 1
    logger.info("Amazon busca '%s': %s upserts, %s fora do mínimo de %s%%",
                termo_busca, total, descartados, min_desconto)
    return total


def _tem_desconto_minimo(m, min_desconto) -> bool:
    de, por = m["preco_sem_desconto"], m["preco_com_cupom"]
    if de <= 0 or por <= 0 or de <= por:
        return False
    return (de - por) / de * 100 >= min_desconto


def mapear_cupons_codigo(usuario=None, itens=None):
    """
    Itens com PROMOÇÃO/cupom de clipar -> origem='cupom_codigo', owner=usuario.
    Reaproveita a coleta do feed; só persiste quem tem promoção na OffersV2.
    Amazon não usa código de checkout, então codigo_checkout fica vazio.
    """
    itens = coletar_feed(usuario) if itens is None else itens
    total = 0
    for m in itens:
        if m["tem_promocao"]:
            _upsert_produto(m, origem="cupom_codigo", owner=usuario)
            total += 1
    logger.info("Amazon cupons/promocoes: %s upserts", total)
    return total
