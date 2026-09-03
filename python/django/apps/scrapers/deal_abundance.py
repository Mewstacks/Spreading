"""Cobertura de deals por regra de envio, com déficit nomeado.

`coupon_abundance` responde "temos cupons?" por loja. Essa pergunta não serve para
o creator: ele não publica cupom, publica deal do nicho dele. Uma conta pode ter mil
cupons prontos e zero deal em "Robô aspirador" — e o relatório de cupons diria que
está tudo bem.

Aqui a unidade é a regra de envio. E, como em `coupon_abundance`, um déficit só é
declarado como escassez do mercado quando as fontes provaram que terminaram a
varredura; enquanto alguma parou por orçamento nosso, o veredito é `coleta_incompleta`
— acusação contra nós, não contra a loja.
"""
from __future__ import annotations

from collections import defaultdict

from django.conf import settings
from django.utils import timezone

from apps.scrapers.coupon_abundance import exaustao_das_fontes


def meta_por_regra() -> int:
    try:
        return max(0, int(getattr(settings, "DEAL_COBERTURA_META_DIA", 10)))
    except (TypeError, ValueError):
        return 10


def _fontes_nao_exauridas(marketplace) -> list:
    """Fontes que pararam por orçamento nosso, e não por fim de inventário."""
    lojas = [marketplace] if marketplace else None
    mapa = exaustao_das_fontes(**({"marketplaces": lojas} if lojas else {}))
    pendentes = []
    for itens in mapa.values():
        pendentes += [
            item["fonte"] for item in itens if item["exaustao"] != "exaurida"
        ]
    return sorted(set(pendentes))


def cobertura_da_regra(config, *, agora=None, meta=None):
    """Deals elegíveis agora para esta regra, e o que segurou os que faltaram."""
    from apps.scrapers.deals import gerar_deals

    agora = agora or timezone.now()
    meta = meta_por_regra() if meta is None else max(0, int(meta))
    rejeicoes = defaultdict(int)
    deals = gerar_deals(config, limite=None, agora=agora, rejeicoes=rejeicoes)
    com_cupom = sum(1 for deal in deals if deal.tem_cupom)
    marketplace = str(getattr(config, "marketplace", "") or "").casefold()
    pendentes = _fontes_nao_exauridas(marketplace)

    if len(deals) >= meta:
        veredito = "meta_atingida"
    elif pendentes:
        veredito = "coleta_incompleta"
    else:
        veredito = "deficit_provado"
    return {
        "config_id": getattr(config, "pk", None),
        "destino": getattr(config, "grupo_nome", "") or getattr(config, "grupo_id", ""),
        "canal": getattr(config, "canal", ""),
        "macro": getattr(config, "macro_categoria", ""),
        "marketplace": marketplace,
        "elegiveis": len(deals),
        "com_cupom": com_cupom,
        "sem_cupom": len(deals) - com_cupom,
        "meta": meta,
        "deficit": max(0, meta - len(deals)),
        "veredito": veredito,
        "deficit_provado": veredito == "deficit_provado",
        "fontes_nao_exauridas": pendentes,
        # Ordenado pelo que mais segurou: é a fila de trabalho, não uma curiosidade.
        "rejeicoes": dict(sorted(
            rejeicoes.items(), key=lambda par: -par[1],
        )),
        "melhor_score": deals[0].score if deals else None,
        "melhor_prova": deals[0].prova if deals else "",
    }


def relatorio_cobertura(*, agora=None, meta=None, apenas_ativas=True, usuario=None):
    """Resposta única para "cada creator tem deal suficiente no nicho dele?"."""
    from apps.scrapers.models import ConfiguracaoEnvio

    agora = agora or timezone.now()
    meta = meta_por_regra() if meta is None else max(0, int(meta))
    consulta = ConfiguracaoEnvio.objects.select_related("owner").filter(
        tipo=ConfiguracaoEnvio.TIPO_OFERTAS,
    )
    if apenas_ativas:
        consulta = consulta.filter(ativo=True)
    if usuario is not None:
        consulta = consulta.filter(owner=usuario)
    regras = [
        cobertura_da_regra(config, agora=agora, meta=meta)
        for config in consulta.order_by("pk")
    ]
    return {
        "gerado_em": agora,
        "meta": meta,
        "regras": regras,
        # Uma regra abaixo da meta reprova o conjunto: um creator sem deal é um
        # creator sem produto, por mais que a média das outras contas esteja boa.
        "aprovado": bool(regras) and all(
            regra["elegiveis"] >= meta for regra in regras
        ),
        "coleta_incompleta": [
            regra["config_id"] for regra in regras
            if regra["veredito"] == "coleta_incompleta"
        ],
    }
