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

from apps.scrapers.models import Produto
from apps.scrapers.scraper_amazon import creators_api
# Reaproveita a classificação por nome (PT) já usada no ML.
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome


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
        preco_de = preco_atual  # sem desconto de/por; pode ainda ter promoção (dealDetails)

    # Promoção/cupom de clipar agora vem em dealDetails (null quando não há).
    deal = _primeiro(listing, ("dealDetails",))
    tem_promocao = bool(deal)
    rotulo_promo = ""
    if isinstance(deal, dict):
        rotulo_promo = (deal.get("displayName") or deal.get("title")
                        or deal.get("badge") or deal.get("type") or "Promoção")[:60]

    # Prime/deliveryInfo não existe mais no catalog/v1. Aproxima "vendido pela Amazon"
    # (merchantInfo.name) como selo de confiança no lugar do antigo frete_full/Prime.
    merchant = _primeiro(listing, ("merchantInfo", "name")) or ""
    prime = "amazon" in merchant.lower()
    imagem = _primeiro(item, ("images", "primary", "large", "url")) or ""
    link = (item.get("detailPageURL") or item.get("detailPageUrl")
            or f"https://{settings.AMAZON_MARKETPLACE}/dp/{asin}")

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
            "link_produto": m["link_produto"],
            "categoria": "DESCONHECIDO",
            "macro_categoria": macro or classificar_oferta_por_nome(m["nome"]),
            "imagem_url": m["imagem_url"],
            "frete_full": m["frete_full"],
            "codigo_checkout": "",  # Amazon: cupom é de clipar, não tem código
        },
    )
    # Histórico de preços (B1): observação por asin p/ medir queda real depois.
    from apps.scrapers.precos import registrar
    registrar("amazon", m["asin"], m["link_produto"], m["preco_com_cupom"])
    return True


def _coletar(keyword, min_savings, max_paginas=2, creds=None):
    """Coleta itens mapeados de uma busca (até max_paginas) com as creds dadas."""
    out = []
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


def mapear_ofertas(usuario=None):
    """Varre keywords de feed com desconto mínimo -> origem='oferta', owner=usuario.
    Itens Amazon são privados do usuário (raspados com a conta Creators dele)."""
    creds = creators_api.creds_de_usuario(usuario)
    min_savings = int(getattr(settings, "AMAZON_MIN_SAVINGS_PCT", 15))
    keywords = getattr(settings, "AMAZON_FEED_KEYWORDS", []) or []
    print(f"[amazon] Ofertas (user={getattr(usuario,'id',None)}): {len(keywords)} keywords, min {min_savings}% off")
    total = 0
    for i, kw in enumerate(keywords, 1):
        print(f"[PROGRESSO] Amazon ofertas {i}/{len(keywords)}: {kw}")
        try:
            for m in _coletar(kw, min_savings, creds=creds):
                # só feed: itens com desconto real de/por
                if m["preco_sem_desconto"] > m["preco_com_cupom"]:
                    _upsert_produto(m, origem="oferta", owner=usuario)
                    total += 1
        except creators_api.AmazonNotEligible:
            raise
        except Exception as e:
            print(f"  keyword '{kw}' falhou: {e}")
    print(f"[amazon] OFERTAS: {total} upserts.")
    return total


def buscar_por_termo(termo_busca, min_desconto=15, max_paginas=2, macro=None, usuario=None):
    """searchItems por sub-nicho (lista separada por vírgula) -> origem='busca', owner=usuario."""
    creds = creators_api.creds_de_usuario(usuario)
    termos = [t.strip() for t in (termo_busca or "").split(",") if t.strip()]
    total = 0
    for termo in termos:
        try:
            for m in _coletar(termo, int(min_desconto), max_paginas, creds=creds):
                _upsert_produto(m, origem="busca", macro=macro, owner=usuario)
                total += 1
        except creators_api.AmazonNotEligible:
            raise
        except Exception as e:
            print(f"  busca amazon '{termo}' falhou: {e}")
    print(f"[amazon] BUSCA '{termo_busca}': {total} upserts.")
    return total


def mapear_cupons_codigo(usuario=None):
    """
    Itens com PROMOÇÃO/cupom de clipar -> origem='cupom_codigo', owner=usuario.
    Reaproveita as keywords de feed; só persiste quem tem promotions na OffersV2.
    Amazon não usa código de checkout, então codigo_checkout fica vazio.
    """
    creds = creators_api.creds_de_usuario(usuario)
    min_savings = int(getattr(settings, "AMAZON_MIN_SAVINGS_PCT", 15))
    keywords = getattr(settings, "AMAZON_FEED_KEYWORDS", []) or []
    total = 0
    for kw in keywords:
        try:
            for m in _coletar(kw, min_savings, creds=creds):
                if m["tem_promocao"]:
                    _upsert_produto(m, origem="cupom_codigo", owner=usuario)
                    total += 1
        except creators_api.AmazonNotEligible:
            raise
        except Exception as e:
            print(f"  cupom amazon '{kw}' falhou: {e}")
    print(f"[amazon] CUPONS (promoções): {total} upserts.")
    return total
