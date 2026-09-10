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


def meta_de_cupom_por_regra() -> int:
    """Quantos dos deals do nicho precisam ter cupom.

    Volume sozinho aprovava um nicho vazio do que importa. Medido em produção em
    07/09/2026, com a meta contando só deals:

        Eletrodomésticos          50 elegíveis   2 com cupom   meta_atingida
        Celulares, Telefonia      26 elegíveis   1 com cupom   meta_atingida
        Ferramentas e Manutenção  37 elegíveis   0 com cupom   meta_atingida

    Trinta e sete ofertas e nenhum cupom passava como "meta atingida". Cupom é o
    produto; promoção é acompanhamento — então a cobertura cobra os dois, e um
    nicho sem cupom nenhum não é um nicho coberto.

    O padrão é 3, e é deliberadamente baixo: nicho específico e de pouco giro não
    precisa de abundância, precisa de existir. Sobe por setting quando o funil de
    cupom voltar a render.
    """
    try:
        return max(0, int(getattr(settings, "DEAL_COBERTURA_META_CUPOM_DIA", 3)))
    except (TypeError, ValueError):
        return 3


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
    # Para a operação da Lu, "tem cupom" não pode significar apenas que a
    # vitrine mostra uma ativação. A mensagem que o público recebeu como padrão
    # editorial precisa trazer um código que ele consiga copiar, ligado àquele
    # produto e àquela URL. Mantemos a contagem ampla para diagnóstico, mas a
    # meta/gate usa exclusivamente o subconjunto com código publicável.
    from apps.scrapers.coupon_rules import codigo_publicavel

    com_cupom = sum(1 for deal in deals if deal.tem_cupom)
    com_cupom_codigo = sum(
        1 for deal in deals
        if deal.tem_cupom and codigo_publicavel(getattr(deal, "cupom", None))
    )
    marketplace = str(getattr(config, "marketplace", "") or "").casefold()
    pendentes = _fontes_nao_exauridas(marketplace)

    meta_cupom = meta_de_cupom_por_regra()
    deficit_cupom = max(0, meta_cupom - com_cupom_codigo)
    if len(deals) >= meta and not deficit_cupom:
        veredito = "meta_atingida"
    elif pendentes:
        veredito = "coleta_incompleta"
    elif deficit_cupom and len(deals) >= meta:
        # Distinguir os dois déficits importa para saber o que consertar: falta de
        # oferta é problema de coleta; oferta sobrando e cupom faltando é o funil
        # de cupom parado, que tem outra causa e outro conserto.
        veredito = "sem_cupom_no_nicho"
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
        "com_cupom_codigo": com_cupom_codigo,
        "sem_cupom": len(deals) - com_cupom,
        "sem_cupom_codigo": len(deals) - com_cupom_codigo,
        "meta": meta,
        "meta_cupom": meta_cupom,
        "deficit": max(0, meta - len(deals)),
        "deficit_cupom": deficit_cupom,
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
            regra["elegiveis"] >= meta and not regra["deficit_cupom"]
            for regra in regras
        ),
        "coleta_incompleta": [
            regra["config_id"] for regra in regras
            if regra["veredito"] == "coleta_incompleta"
        ],
    }
