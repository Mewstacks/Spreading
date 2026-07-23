"""Resolução de identidade de afiliado + cache de link POR usuário.

No Mercado Livre a identidade vem exclusivamente da conta autenticada no Link
Builder. A tag textual só existe no fluxo Amazon.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone


def tag_ml(usuario=None) -> str:
    """Compatibilidade: ML não possui tag manual separada neste fluxo."""
    return ""


def tag_amazon(usuario=None) -> str:
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.afiliado_tag_amazon:
            return perfil.afiliado_tag_amazon.strip()
        return ""
    return (getattr(settings, "AMAZON_PARTNER_TAG", "") or "").strip()


def link_cacheado(usuario, produto):
    """Retorna o LinkAfiliadoUsuario (usuario, produto) ou None."""
    if usuario is None:
        return None
    from apps.scrapers.models import LinkAfiliadoUsuario
    return LinkAfiliadoUsuario.objects.filter(usuario=usuario, produto=produto).first()


def ids_com_link(usuario, produtos) -> set:
    """IDs dos produtos que já têm link pronto para este usuário — UMA query.

    Versão em lote de link_cacheado, para quem precisa do predicado de uma página
    inteira de produtos (a listagem) sem fazer uma query por item.
    """
    if usuario is None or not produtos:
        return set()
    from apps.scrapers.models import LinkAfiliadoUsuario
    return set(
        LinkAfiliadoUsuario.objects
        .filter(usuario=usuario, produto__in=produtos)
        .exclude(link_afiliado="")
        .values_list("produto_id", flat=True)
    )


def situacao_dos_links(usuario, produtos) -> dict:
    """{produto_id: {estado, ultimo_erro, tentativas, link_afiliado}} — UMA query.

    Superset de ids_com_link: traz também os itens SEM link, que é o que permite a
    listagem distinguir "na fila" de "não afiliável" em vez de chamar tudo de
    "pendente". Quem precisa das duas coisas deve chamar só esta e derivar os
    prontos daqui — a listagem tem orçamento de uma query por loja, não duas.
    """
    if usuario is None or not produtos:
        return {}
    from apps.scrapers.models import LinkAfiliadoUsuario
    return {
        linha["produto_id"]: linha
        for linha in LinkAfiliadoUsuario.objects
        .filter(usuario=usuario, produto__in=produtos)
        .values("produto_id", "estado", "ultimo_erro", "tentativas", "link_afiliado",
                "verificado_ok", "url_canonica", "verificacao_motivo")
    }


def resumo_afiliacao(usuario) -> dict:
    """Contagem honesta do catálogo visível ao usuário, separando fila de terminal.

    A tela somava tudo que não tinha link num único "sem link" — e um catálogo
    saudável, com só algumas centenas de itens realmente fora do Programa, parecia
    milhares de falhas. Aqui "pendente" (na fila, o link vem) é distinto de "não
    afiliável" e "erro" (terminais, o link não vem).
    """
    from django.db.models import Count, Q
    from apps.scrapers.models import Produto, LinkAfiliadoUsuario

    escopo = Produto.objects.filter(Q(owner__isnull=True) | Q(owner=usuario))
    total = escopo.count()
    linhas = LinkAfiliadoUsuario.objects.filter(usuario=usuario, produto__in=escopo)
    # "Pronto" = enviável de verdade: tem link E o destino já foi aprovado
    # (verificado_ok=True). Contar só a existência do link era o que fazia a tela
    # prometer envio para links que só reprovavam no clique de enviar.
    contagens = linhas.aggregate(
        prontos=Count("id", filter=~Q(link_afiliado="") & Q(verificado_ok=True)),
        nao_afiliavel=Count("id", filter=Q(estado="nao_afiliavel")),
        erro=Count("id", filter=Q(estado="erro")),
    )
    prontos = contagens["prontos"] or 0
    nao_afiliavel = contagens["nao_afiliavel"] or 0
    erro = contagens["erro"] or 0
    # Legado: itens antigos com link no próprio Produto (pré-multi-tenant), sem
    # linha em LinkAfiliadoUsuario.
    legacy = escopo.exclude(link_afiliado="").exclude(
        id__in=linhas.exclude(link_afiliado="").values("produto_id")).count()
    prontos += legacy
    pendente = max(total - prontos - nao_afiliavel - erro, 0)
    return {"total": total, "prontos": prontos, "pendente": pendente,
            "nao_afiliavel": nao_afiliavel, "erro": erro}


def frase_resumo_afiliacao(usuario) -> str:
    """Uma linha legível a partir de resumo_afiliacao — para o log SSE pós-raspagem."""
    r = resumo_afiliacao(usuario)
    partes = [f"{r['pendente']} aguardando link", f"{r['prontos']} prontos"]
    if r["nao_afiliavel"]:
        partes.append(f"{r['nao_afiliavel']} não afiliáveis (catálogo/URL fora do Programa)")
    if r["erro"]:
        partes.append(f"{r['erro']} com erro")
    return "Afiliação: " + " · ".join(partes) + "."


def salvar_cache(usuario, produto, link_afiliado, url_isca, afiliado_ok,
                 verificado_ok=None, url_canonica="", verificacao_motivo="") -> None:
    """Persiste o link gerado e, quando disponível, o veredito de verificação.

    `estado='pronto'` significa apenas "o link foi gerado". A ENVIABILIDADE mora em
    `verificado_ok`: só quando ele é True a listagem oferece o envio. Gerar (ou
    regerar) um link sem veredito recente deixa `verificado_ok=None` — o item fica
    "verificando" até a checagem de destino aprovar, nunca enviável no escuro.
    """
    if usuario is None or not link_afiliado:
        return
    from apps.scrapers.models import LinkAfiliadoUsuario
    LinkAfiliadoUsuario.objects.update_or_create(
        usuario=usuario, produto=produto,
        defaults={"link_afiliado": link_afiliado, "url_isca": url_isca,
                  "afiliado_ok": afiliado_ok, "estado": "pronto",
                  "ultimo_erro": "", "proxima_tentativa": None,
                  "ultima_tentativa": timezone.now(),
                  "verificado_ok": verificado_ok,
                  "verificado_em": timezone.now() if verificado_ok is not None else None,
                  "url_canonica": url_canonica or (link_afiliado if verificado_ok else ""),
                  "verificacao_motivo": verificacao_motivo},
    )


def registrar_aprovacao(usuario, produto, link_afiliado, url_canonica="") -> None:
    """Marca um link JÁ cacheado como verificado e enviável (veredito=True).

    Usado quando a verificação de destino roda separada da geração (ex.: backfill
    ou reverificação): não regera o link, só carimba o veredito e a URL canônica
    que o envio deve usar.
    """
    if usuario is None or produto is None:
        return
    from apps.scrapers.models import LinkAfiliadoUsuario
    LinkAfiliadoUsuario.objects.filter(usuario=usuario, produto=produto).update(
        verificado_ok=True, verificado_em=timezone.now(),
        url_canonica=url_canonica or "", verificacao_motivo="", estado="pronto")


def registrar_reprovacao(usuario, produto, motivo: str) -> None:
    """Marca o link como reprovado na verificação de destino (veredito=False).

    O item deixa de ser enviável na tela e passa a exibir o motivo — em vez de o
    usuário só descobrir depois de clicar em enviar. Não apaga o link_afiliado: a
    próxima regeração pode substituí-lo e voltar a verificar.
    """
    if usuario is None or produto is None:
        return
    from apps.scrapers.models import LinkAfiliadoUsuario
    LinkAfiliadoUsuario.objects.filter(usuario=usuario, produto=produto).update(
        verificado_ok=False, verificado_em=timezone.now(),
        url_canonica="", verificacao_motivo=(motivo or "")[:300])


# Backoff entre tentativas de afiliar o mesmo produto. Antes não havia nenhum: o
# item voltava ao lote a cada 5min, para sempre. Como o lote é de 40 e a fila é
# ordenada pelos mais recentes, um punhado de produtos que nunca afiliam ocupava o
# lote inteiro a cada ciclo e nenhum outro produto avançava — a pilha de "pendente"
# que não saía mesmo com o worker rodando.
_BACKOFF_MIN = (5, 15, 60, 180, 360)
MAX_TENTATIVAS_ERRO = 8


def registrar_falha(usuario, produto, motivo: str, *, terminal: bool = False) -> None:
    """Grava POR QUE este produto não tem link, e quando tentar de novo.

    `terminal` para causas que retentar não resolve (URL fora do Programa). Elas
    saem da fila de vez: não são "pendente", são "não afiliável", e a tela precisa
    dizer isso em vez de prometer um link que nunca vem.
    """
    if usuario is None or produto is None:
        return
    from apps.scrapers.models import LinkAfiliadoUsuario

    agora = timezone.now()
    linha, _ = LinkAfiliadoUsuario.objects.get_or_create(
        usuario=usuario, produto=produto,
        defaults={"ultima_tentativa": agora},
    )
    if linha.link_afiliado:
        return                                  # já tem link; falha superveniente é ruído
    linha.tentativas += 1
    linha.ultimo_erro = (motivo or "")[:300]
    linha.ultima_tentativa = agora
    if terminal:
        linha.estado, linha.proxima_tentativa = "nao_afiliavel", None
    elif linha.tentativas >= MAX_TENTATIVAS_ERRO:
        # Desistir também é honesto: 8 idas ao Link Builder falhando é sinal de que
        # o problema não é transitório. Some da fila e a tela mostra o motivo.
        linha.estado, linha.proxima_tentativa = "erro", None
    else:
        minutos = _BACKOFF_MIN[min(linha.tentativas - 1, len(_BACKOFF_MIN) - 1)]
        linha.estado = "pendente"
        linha.proxima_tentativa = agora + timedelta(minutes=minutos)
    linha.save(update_fields=["tentativas", "ultimo_erro", "ultima_tentativa",
                              "estado", "proxima_tentativa"])
