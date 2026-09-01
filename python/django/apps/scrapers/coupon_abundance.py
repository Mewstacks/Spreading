"""Abundância de cupons por marketplace, contra a meta, com déficit provado.

O contrato de aceite (``PLANO_REVISAO_PRODUCAO.md``) pede ao menos 100 cupons
distintos prontos para Mercado Livre, Amazon e Shopee — e diz, no mesmo parágrafo,
que a meta é condicionada ao inventário realmente observável: "quando uma loja não
expuser a quantidade mínima, o sistema deverá provar a exaustão das fontes e exibir
o déficit explicitamente".

Este módulo é esse instrumento. Ele não muda o funil; ele mede três coisas que
antes ninguém conseguia responder sem abrir o banco:

1. quantos cupons DISTINTOS estão prontos por loja (nenhum total agregado
   compensa uma loja abaixo da meta);
2. qual o déficit e o que está segurando os que não chegaram lá;
3. se as fontes daquela loja realmente terminaram a varredura — porque um déficit
   com fonte parada em ``max_pages`` não prova ausência de inventário, prova
   coleta incompleta, e a diferença entre os dois é o que separa "a loja não tem"
   de "nós não fomos buscar".
"""
from __future__ import annotations

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.scrapers.maintenance import cupons_frescos_q


# As três lojas do recorte competitivo. Awin e privadas continuam existindo no
# funil; elas simplesmente não fazem parte desta meta.
MARKETPLACES_META = ("mercadolivre", "amazon", "shopee")

# Motivos de parada que provam fim real da varredura. `no_new_items` é a rodada
# estável (o adaptador viu três voltas sem nada novo); `healthy_empty` é a fonte
# que declarou vazio com schema íntegro.
PARADAS_EXAUSTIVAS = frozenset({"no_new_items", "end_of_source", "healthy_empty"})
# Motivos de parada que são orçamento nosso, não fim do inventário da loja.
PARADAS_POR_ORCAMENTO = frozenset({"max_pages", "max_items", "max_seconds"})


def meta_por_marketplace() -> int:
    try:
        return max(0, int(getattr(settings, "COUPON_ABUNDANCE_GOAL", 100)))
    except (TypeError, ValueError):
        return 100


def meta_descoberta_diaria() -> int:
    try:
        return max(
            0, int(getattr(settings, "COUPON_DAILY_DISCOVERY_GOAL", 250)),
        )
    except (TypeError, ValueError):
        return 250


def _base_projecoes(*, agora, channel):
    from apps.scrapers.models import CupomDisponibilidade

    return CupomDisponibilidade.objects.filter(
        channel=channel, cupom__estado="ativo",
    ).filter(cupons_frescos_q(agora=agora, prefix="cupom__"))


def prontos_distintos(*, agora=None, channel="whatsapp", usuario=None):
    """Cupons DISTINTOS em ``ready`` por marketplace e por modo de uso.

    Distintos por ``cupom_id``: a projeção tem uma linha por (organização,
    usuário, cupom, canal, modo), então contar linhas multiplicaria o mesmo cupom
    pelo número de usuários e faria a meta parecer atingida com um catálogo
    pequeno. A meta fala em cupons, não em projeções.
    """
    agora = agora or timezone.now()
    consulta = _base_projecoes(agora=agora, channel=channel).filter(stage="ready")
    if usuario is not None:
        consulta = consulta.filter(usuario=usuario)
    resultado = {
        marketplace: {"total": 0, "por_modo": {}}
        for marketplace in MARKETPLACES_META
    }
    linhas = (
        consulta.values("cupom__marketplace", "use_mode")
        .annotate(total=Count("cupom_id", distinct=True))
    )
    for linha in linhas:
        marketplace = str(linha["cupom__marketplace"] or "").casefold()
        alvo = resultado.setdefault(marketplace, {"total": 0, "por_modo": {}})
        alvo["por_modo"][linha["use_mode"]] = linha["total"]
    # O total por loja é recontado separadamente: somar os modos contaria duas
    # vezes o cupom que está pronto como código E como ativação.
    totais = (
        consulta.values("cupom__marketplace")
        .annotate(total=Count("cupom_id", distinct=True))
    )
    for linha in totais:
        marketplace = str(linha["cupom__marketplace"] or "").casefold()
        resultado.setdefault(marketplace, {"total": 0, "por_modo": {}})
        resultado[marketplace]["total"] = linha["total"]
    return resultado


def bloqueios_por_marketplace(*, agora=None, channel="whatsapp", limite=8,
                              usuario=None):
    """O que segura os cupons que NÃO estão prontos, por loja.

    Sem isto, o déficit é um número sem endereço. Com isto, ele vira uma lista de
    ações: "44 esperando corroboração", "79 sem integração Shopee".
    """
    agora = agora or timezone.now()
    consulta = _base_projecoes(agora=agora, channel=channel).exclude(stage="ready")
    if usuario is not None:
        consulta = consulta.filter(usuario=usuario)
    resultado = {marketplace: [] for marketplace in MARKETPLACES_META}
    linhas = (
        consulta.values("cupom__marketplace", "stage", "category", "reason_code")
        .annotate(total=Count("cupom_id", distinct=True))
        .order_by("-total")
    )
    for linha in linhas:
        marketplace = str(linha["cupom__marketplace"] or "").casefold()
        alvo = resultado.setdefault(marketplace, [])
        if len(alvo) >= limite:
            continue
        alvo.append({
            "stage": linha["stage"],
            "category": linha["category"],
            "reason_code": linha["reason_code"],
            "cupons": linha["total"],
        })
    return resultado


def descoberta_24h(*, agora=None, janela_horas=24):
    """Candidatos novos ou reobservados por marketplace na janela, e classes de fonte.

    A meta de descoberta pede 250 por marketplace vindos de pelo menos três classes
    de fonte. As duas metades importam: 250 candidatos de um agregador só não é
    descoberta diversificada, é uma fonte falando alto.
    """
    from datetime import timedelta

    from apps.scrapers.models import CupomFonteObservacao

    agora = agora or timezone.now()
    corte = agora - timedelta(hours=max(1, int(janela_horas)))
    resultado = {
        marketplace: {"candidatos": 0, "por_fonte": {}, "classes": set()}
        for marketplace in MARKETPLACES_META
    }
    linhas = (
        CupomFonteObservacao.objects
        .filter(
            observed_at__gte=corte,
            cupom__marketplace__in=MARKETPLACES_META,
        )
        .values("cupom__marketplace", "fonte__slug")
        .annotate(total=Count("canonical_key", distinct=True))
    )
    for linha in linhas:
        marketplace = str(linha["cupom__marketplace"] or "").casefold()
        alvo = resultado.setdefault(
            marketplace, {"candidatos": 0, "por_fonte": {}, "classes": set()},
        )
        alvo["por_fonte"][linha["fonte__slug"]] = linha["total"]
        alvo["classes"].add(classe_da_fonte(linha["fonte__slug"]))
    for marketplace, dados in resultado.items():
        # Distinto por fonte, somado entre fontes: o mesmo código visto por duas
        # fontes é descoberta das duas, e é justamente isso que corrobora.
        dados["candidatos"] = sum(dados["por_fonte"].values())
        dados["classes"] = sorted(dados["classes"])
    return resultado


# As três classes que o contrato exige por marketplace. `classe_da_fonte` é a
# fronteira única: sem ela, cada relatório inventava a própria taxonomia.
CLASSES_DE_FONTE = ("oficial", "agregador", "comunidade")
_FONTES_AGREGADOR = frozenset({
    "promobit-cupons", "meliuz-cupons", "pelando-cupons",
    "bia-garimpa-cupons", "cupomspot-cupons",
})
_FONTES_COMUNIDADE_DIRETA = frozenset({
    "telegram-publico", "promobit-community", "pelando-community",
})


def classe_da_fonte(slug) -> str:
    slug = str(slug or "")
    if slug in _FONTES_COMUNIDADE_DIRETA:
        return "comunidade"
    if slug in _FONTES_AGREGADOR:
        return "agregador"
    return "oficial"


def _exaustao_da_execucao(execucao):
    """Esta execução prova que a fonte foi até o fim?

    Três respostas possíveis, e a do meio é a que importa: ``incompleta`` é a
    fonte que parou por orçamento nosso. Enquanto houver uma dessas, o déficit da
    loja não pode ser declarado como ausência de inventário.
    """
    metricas = execucao.metricas if isinstance(execucao.metricas, dict) else {}
    parada = str(metricas.get("stop_reason") or "")
    saude = str(execucao.health_status or "")
    if saude in {"blocked", "degraded"} or execucao.status in {"error", "blocked"}:
        return "bloqueada", parada
    if metricas.get("complete") is True or parada in PARADAS_EXAUSTIVAS:
        return "exaurida", parada
    if parada in PARADAS_POR_ORCAMENTO:
        return "incompleta", parada
    if saude == "healthy_empty":
        return "exaurida", parada or "healthy_empty"
    return "desconhecida", parada


def exaustao_das_fontes(*, marketplaces=MARKETPLACES_META):
    """Última execução de cada fonte habilitada, classificada por exaustão."""
    from django.db.models import OuterRef, Subquery
    from apps.scrapers.models import ExecucaoIngestao, FonteIngestao

    fontes = FonteIngestao.objects.filter(
        marketplace__in=tuple(marketplaces) + ("multiloja",), habilitada=True,
    ).order_by("marketplace", "slug")
    # O histórico cresce a cada ciclo (flash: a cada cinco minutos). Ler todas as
    # execuções, ordenar e descartar em Python levou 10,9 s em produção só para
    # responder qual foi a última de cada fonte. O subselect correlacionado mantém
    # a mesma semântica e traz no máximo uma linha por fonte.
    ultima_id_da_fonte = (
        ExecucaoIngestao.objects
        .filter(fonte_id=OuterRef("fonte_id"))
        .order_by("-iniciada_em", "-pk")
        .values("pk")[:1]
    )
    ultimas = {
        execucao.fonte_id: execucao
        for execucao in ExecucaoIngestao.objects.filter(
            fonte__in=fontes, pk=Subquery(ultima_id_da_fonte),
        ).select_related("fonte")
    }

    resultado = {marketplace: [] for marketplace in marketplaces}
    for fonte in fontes:
        execucao = ultimas.get(fonte.pk)
        if execucao is None:
            item = {
                "fonte": fonte.slug, "exaustao": "nunca_executada",
                "stop_reason": "", "status": fonte.status,
                "health": "", "aceitos": 0, "vistos": 0, "quando": None,
            }
            destinos = marketplaces if fonte.marketplace == "multiloja" else (fonte.marketplace,)
            for marketplace in destinos:
                resultado.setdefault(marketplace, []).append(dict(item))
            continue
        exaustao, parada = _exaustao_da_execucao(execucao)
        metricas = execucao.metricas if isinstance(execucao.metricas, dict) else {}
        item = {
            "fonte": fonte.slug,
            "exaustao": exaustao,
            "stop_reason": parada,
            "status": execucao.status,
            "health": execucao.health_status,
            "aceitos": int(metricas.get("accepted", execucao.total_cupons) or 0),
            "vistos": int(metricas.get("items_seen", metricas.get("cards_seen", 0)) or 0),
            "quando": execucao.finalizada_em or execucao.iniciada_em,
        }
        destinos = marketplaces if fonte.marketplace == "multiloja" else (fonte.marketplace,)
        for marketplace in destinos:
            resultado.setdefault(marketplace, []).append(dict(item))
    return resultado


def relatorio_abundancia(*, agora=None, channel="whatsapp", usuario=None,
                         meta=None):
    """Resposta única para "temos cupons suficientes nas três lojas?".

    ``deficit_provado`` é a palavra cara. Ela só fica verdadeira quando a loja
    está abaixo da meta E todas as fontes habilitadas terminaram a varredura. Com
    qualquer fonte parada por orçamento ou bloqueada, o veredito é
    ``coleta_incompleta`` — que é uma acusação contra nós, não contra a loja.
    """
    agora = agora or timezone.now()
    meta = meta_por_marketplace() if meta is None else max(0, int(meta))
    prontos = prontos_distintos(agora=agora, channel=channel, usuario=usuario)
    bloqueios = bloqueios_por_marketplace(
        agora=agora, channel=channel, usuario=usuario,
    )
    fontes = exaustao_das_fontes()
    descoberta = descoberta_24h(agora=agora)
    meta_descoberta = meta_descoberta_diaria()

    lojas = {}
    for marketplace in MARKETPLACES_META:
        total = prontos.get(marketplace, {}).get("total", 0)
        fontes_da_loja = fontes.get(marketplace, [])
        nao_exauridas = [
            item for item in fontes_da_loja if item["exaustao"] != "exaurida"
        ]
        atinge = total >= meta
        if atinge:
            veredito = "meta_atingida"
        elif not fontes_da_loja:
            veredito = "sem_fonte_habilitada"
        elif nao_exauridas:
            veredito = "coleta_incompleta"
        else:
            veredito = "deficit_provado"
        lojas[marketplace] = {
            "prontos": total,
            "por_modo": prontos.get(marketplace, {}).get("por_modo", {}),
            "meta": meta,
            "deficit": max(0, meta - total),
            "veredito": veredito,
            "deficit_provado": veredito == "deficit_provado",
            "fontes": fontes_da_loja,
            "fontes_nao_exauridas": [item["fonte"] for item in nao_exauridas],
            "bloqueios": bloqueios.get(marketplace, []),
            "descoberta_24h": descoberta.get(marketplace, {}).get("candidatos", 0),
            "meta_descoberta_24h": meta_descoberta,
            "classes_descoberta": descoberta.get(marketplace, {}).get("classes", []),
            "fontes_descoberta": descoberta.get(marketplace, {}).get("por_fonte", {}),
            "descoberta_atingida": (
                descoberta.get(marketplace, {}).get("candidatos", 0)
                >= meta_descoberta
                and len(descoberta.get(marketplace, {}).get("classes", [])) >= 3
            ),
        }
    return {
        "gerado_em": agora,
        "canal": channel,
        "meta": meta,
        "lojas": lojas,
        # Uma loja abaixo da meta reprova o conjunto: o contrato diz que nenhum
        # total agregado compensa marketplace abaixo da meta.
        "aprovado": all(
            dados["prontos"] >= meta and dados["descoberta_atingida"]
            for dados in lojas.values()
        ),
    }
