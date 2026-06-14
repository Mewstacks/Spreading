"""
Links de afiliado da Amazon — puro Python, sem browser.

Diferente do ML (que precisa do Link Builder via Playwright), o link de afiliado da
Amazon é só a URL canônica do produto com a tag do associado:
    https://www.amazon.com.br/dp/{ASIN}?tag={AMAZON_PARTNER_TAG}

Por isso build/prefetch são instantâneos e batcháveis. A verificação A3 confere
apenas que a tag está presente na URL (sem rede).
"""
from urllib.parse import urlencode, urlparse, parse_qs

from django.conf import settings


def _tag(usuario=None) -> str:
    from apps.scrapers.afiliado import tag_amazon
    return tag_amazon(usuario)


def _asin_de(produto):
    asin = getattr(produto, "asin", "") if hasattr(produto, "asin") else produto.get("asin", "")
    return (asin or "").strip()


def _url_canonica(produto) -> str:
    """URL canônica /dp/{ASIN} (preferida) ou o link_produto salvo."""
    asin = _asin_de(produto)
    if asin:
        host = getattr(settings, "AMAZON_MARKETPLACE", "www.amazon.com.br")
        return f"https://{host}/dp/{asin}"
    return (getattr(produto, "link_produto", "") if hasattr(produto, "link_produto")
            else produto.get("link_produto", "")) or ""


def link_tem_tag_afiliado(link: str, usuario=None) -> bool:
    """A3 — True se o link carrega a tag de afiliado (do usuário, ou global). Sem rede."""
    tag = _tag(usuario)
    if not link or not tag:
        return False
    try:
        qs = parse_qs(urlparse(link).query)
    except Exception:
        return False
    return tag in qs.get("tag", [])


def gerar_link_afiliado_para_produto(produto, usuario=None):
    """
    Monta o link de afiliado da Amazon com a tag do usuário (ou global). Retorna dict
    no mesmo formato do ML (link_afiliado/afiliado_ok/url_isca/...) ou None.

    usuario != None -> usa a tag do Perfil e cacheia em LinkAfiliadoUsuario (não toca
    no cache global do Produto, que é por-tag). usuario == None -> comportamento antigo.
    """
    tag = _tag(usuario)
    if not tag:
        print("[amazon-link] tag de afiliado Amazon não configurada (Perfil/settings).")
        return None

    base = _url_canonica(produto)
    if not base:
        print("[amazon-link] Produto sem ASIN/link_produto.")
        return None

    sep = "&" if "?" in base else "?"
    link_afiliado = f"{base}{sep}{urlencode({'tag': tag})}"
    url_isca = base

    if usuario is not None:
        # Multi-tenant: cache por usuário; não sobrescreve o cache global do Produto.
        from apps.scrapers.afiliado import salvar_cache
        salvar_cache(usuario, produto, link_afiliado, url_isca, True)
    elif hasattr(produto, "save"):
        produto.url_isca = url_isca
        produto.link_afiliado = link_afiliado
        produto.afiliado_ok = True
        produto.save(update_fields=["url_isca", "link_afiliado", "afiliado_ok"])

    return {
        "link_afiliado": link_afiliado,
        "afiliado_ok": True,
        "produto_nome": getattr(produto, "nome", "") if hasattr(produto, "nome") else produto.get("nome", ""),
        "preco_vitrine": getattr(produto, "preco_sem_desconto", 0) if hasattr(produto, "preco_sem_desconto") else produto.get("preco_sem_desconto", 0),
        "preco_com_cupom": getattr(produto, "preco_com_cupom", 0) if hasattr(produto, "preco_com_cupom") else produto.get("preco_com_cupom", 0),
        "cupom_titulo": "",
        "url_isca": url_isca,
    }


def gerar_links_em_lote(produtos):
    """Pré-gera links (puro Python). Retorna (gerados, falhas)."""
    gerados = 0
    falhas = 0
    for prod in produtos:
        if getattr(prod, "link_afiliado", ""):
            continue
        try:
            if gerar_link_afiliado_para_produto(prod):
                gerados += 1
            else:
                falhas += 1
        except Exception as e:
            print(f"[amazon-link] Falha ASIN {getattr(prod, 'asin', '?')}: {e}")
            falhas += 1
    return (gerados, falhas)
