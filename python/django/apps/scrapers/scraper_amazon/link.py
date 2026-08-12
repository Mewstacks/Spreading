"""
Links de afiliado da Amazon — puro Python, sem browser.

Diferente do ML (que precisa do Link Builder via Playwright), o link de afiliado da
Amazon é só a URL canônica do produto com a tag do associado:
    https://www.amazon.com.br/dp/{ASIN}?tag={AMAZON_PARTNER_TAG}

Por isso build/prefetch são instantâneos e batcháveis. A verificação A3 confere
apenas que a tag está presente na URL (sem rede).
"""
import logging
from urllib.parse import urlencode, urlparse, parse_qs

from django.conf import settings

logger = logging.getLogger(__name__)


def _tag(usuario=None) -> str:
    from apps.scrapers.afiliado import tag_amazon
    # A tag mora no Perfil (banco): com o tenant apenas anotado (job SSE sem
    # transação presa) a leitura precisa reinstalar o escopo; no worker de
    # sistema o helper cai no caminho direto de sempre.
    from apps.accounts.tenant import executar_orm_ou_direto
    return executar_orm_ou_direto(tag_amazon, usuario)


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


def pode_gerar_link(produto, usuario=None) -> bool:
    """True quando o link comissionado já é montável: tag do Perfil + URL canônica.

    Espelha as pré-condições de gerar_link_afiliado_para_produto sem executá-la (nem
    escrever cache): a UI precisa saber se o item comissiona antes de qualquer envio.
    """
    return bool(_tag(usuario) and _url_canonica(produto))


def gerar_link_afiliado_para_produto(produto, usuario=None):
    """
    Monta o link de afiliado da Amazon com a tag do usuário (ou global). Retorna dict
    no mesmo formato do ML (link_afiliado/afiliado_ok/url_isca/...) ou None.

    usuario != None -> usa a tag do Perfil e cacheia em LinkAfiliadoUsuario (não toca
    no cache global do Produto, que é por-tag). usuario == None -> comportamento antigo.
    """
    tag = _tag(usuario)
    if not tag:
        logger.info("Tag de afiliado Amazon nao configurada")
        return None

    base = _url_canonica(produto)
    if not base:
        logger.info("Produto Amazon sem ASIN/link_produto")
        return None

    sep = "&" if "?" in base else "?"
    link_afiliado = f"{base}{sep}{urlencode({'tag': tag})}"
    url_isca = base

    if usuario is not None:
        # Multi-tenant: cache por usuário; não sobrescreve o cache global do Produto.
        from apps.scrapers.afiliado import salvar_cache
        from apps.accounts.tenant import executar_orm_ou_direto
        # Na Amazon a verificação é determinística e local: a URL canônica acabou
        # de ser montada com a tag cadastrada do próprio usuário. Persistir o mesmo
        # contrato de ``verificado_ok`` usado pelo ML evita que um link válido fique
        # eternamente na etapa "aguardando verificação".
        executar_orm_ou_direto(
            salvar_cache,
            usuario, produto, link_afiliado, url_isca, True,
            verificado_ok=link_tem_tag_afiliado(link_afiliado, usuario=usuario),
            url_canonica=link_afiliado,
            verificacao_motivo="tag Amazon validada localmente",
        )
    elif hasattr(produto, "save"):
        from apps.accounts.tenant import executar_orm_ou_direto
        produto.url_isca = url_isca
        produto.link_afiliado = link_afiliado
        produto.afiliado_ok = True
        executar_orm_ou_direto(
            produto.save,
            update_fields=["url_isca", "link_afiliado", "afiliado_ok"])

    return {
        "link_afiliado": link_afiliado,
        "afiliado_ok": True,
        "produto_nome": getattr(produto, "nome", "") if hasattr(produto, "nome") else produto.get("nome", ""),
        "preco_vitrine": getattr(produto, "preco_sem_desconto", 0) if hasattr(produto, "preco_sem_desconto") else produto.get("preco_sem_desconto", 0),
        "preco_com_cupom": getattr(produto, "preco_com_cupom", 0) if hasattr(produto, "preco_com_cupom") else produto.get("preco_com_cupom", 0),
        "cupom_titulo": "",
        "url_isca": url_isca,
    }


# Reprovação que fala do PRODUTO, não do link: o anúncio saiu do ar. É a única
# reprovação legítima que a Amazon produz hoje, então é a única que o reparo
# preserva — todo o resto em item Amazon veio do verificador errado.
_MOTIVOS_LEGITIMOS = ("indisponív", "indisponiv")


def link_coerente(link: str, produto, usuario=None) -> bool:
    """True quando o link já é EXATAMENTE o que a Amazon exige para comissionar.

    Verificação determinística e local (sem rede): a URL tem de carregar a tag do
    usuário e apontar para o ASIN do próprio produto. É o mesmo par que
    `gerar_link_afiliado_para_produto` monta — por isso serve tanto para aprovar
    quanto para decidir que a URL guardada precisa ser refeita.
    """
    if not link or not link_tem_tag_afiliado(link, usuario=usuario):
        return False
    asin = _asin_de(produto).upper()
    if not asin:
        # Sem ASIN o link canônico é o `link_produto`; comparar host+path basta.
        base = _url_canonica(produto)
        return bool(base and link.startswith(base))
    return asin in link.upper()


def verificar_links_pendentes(usuario, limite=40, produto_ids=None,
                              incluir_reprovados=False) -> dict:
    """Dá veredito aos links Amazon sem abrir navegador nem tocar em outra loja.

    A Amazon não precisa (e não deve) passar pelo verificador de destino do
    Mercado Livre: o link comissionado é determinístico — URL canônica do ASIN mais
    a tag do próprio usuário. Conferir isso é aritmética de string, não navegação.

    `incluir_reprovados` reabre também as linhas já marcadas como reprovadas ou
    terminais. É o modo do reparo: durante o período em que o verificador do ML
    processava links Amazon, links perfeitos foram carimbados como inválidos e
    alguns chegaram a `nao_afiliavel`. Reprovação legítima da própria Amazon
    (anúncio indisponível) é preservada.
    """
    from django.db.models import Q
    from django.utils import timezone

    from apps.scrapers.models import LinkAfiliadoUsuario

    resultado = {"aprovados": 0, "reprovados": 0, "transitorios": 0,
                 "regerados": 0, "bloqueados": 0, "reason_code": ""}
    if usuario is None:
        return resultado

    base = LinkAfiliadoUsuario.objects.filter(
        usuario=usuario, produto__marketplace="amazon",
    )
    if produto_ids is not None:
        base = base.filter(produto_id__in=list(produto_ids))

    if not _tag(usuario):
        # Problema de CONTA, não dos itens: contabiliza uma vez e sai sem gastar
        # tentativa de nenhum produto. Antes, centenas de linhas acumulavam falha
        # individual por uma tag que ninguém cadastrou.
        resultado["bloqueados"] = base.filter(
            Q(verificado_ok__isnull=True) | Q(verificado_ok=False),
        ).count()
        resultado["reason_code"] = "amazon_tag_missing"
        return resultado

    agora = timezone.now()
    pendentes = base.filter(Q(verificado_ok__isnull=True) | Q(verificado_ok=False))
    if not incluir_reprovados:
        pendentes = pendentes.exclude(estado="nao_afiliavel").filter(
            Q(proxima_tentativa__isnull=True) | Q(proxima_tentativa__lte=agora),
        )
    linhas = list(
        pendentes.select_related("produto").order_by("id")[:max(1, int(limite))]
    )
    for linha in linhas:
        motivo = (linha.verificacao_motivo or "").casefold()
        if (linha.verificado_ok is False
                and any(m in motivo for m in _MOTIVOS_LEGITIMOS)):
            # Reprovação de produto indisponível continua valendo.
            resultado["reprovados"] += 1
            continue
        if link_coerente(linha.link_afiliado, linha.produto, usuario=usuario):
            _aprovar_local(linha)
            resultado["aprovados"] += 1
            continue
        # URL ausente ou malformada: refaz a partir do ASIN e da tag atuais.
        if gerar_link_afiliado_para_produto(linha.produto, usuario=usuario):
            resultado["regerados"] += 1
            resultado["aprovados"] += 1
        else:
            resultado["transitorios"] += 1
    return resultado


def _aprovar_local(linha) -> None:
    """Carimba o veredito determinístico e limpa backoff/motivo herdados."""
    from django.utils import timezone

    from apps.scrapers.models import LinkAfiliadoUsuario

    LinkAfiliadoUsuario.objects.filter(pk=linha.pk).update(
        verificado_ok=True, verificado_em=timezone.now(),
        url_canonica=linha.link_afiliado, verificacao_motivo="",
        estado="pronto", ultimo_erro="", proxima_tentativa=None,
    )


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
            logger.warning("Falha ao gerar link Amazon para ASIN %s: %s", getattr(prod, "asin", "?"), e)
            falhas += 1
    return (gerados, falhas)
