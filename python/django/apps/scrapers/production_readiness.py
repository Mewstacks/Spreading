"""Gates read-only para liberar a operação da lules em produção.

Não é um dashboard otimista: cada gate abaixo é uma condição necessária para o
canário privado e a publicação real. O módulo só lê dados; correções, destinos,
credenciais e o envio do canário continuam sendo ações deliberadas do dono.
"""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from apps.scrapers.alertas import _destinos
from apps.scrapers.maintenance import produtos_frescos_q


NICHOS_PRIORITARIOS_LU = frozenset({
    "Eletrodomésticos",
    "Limpeza e Lavanderia",
    "Cozinha, Mesa e Bar",
    "Casa, Móveis e Decoração",
})
_MARCADORES_DE_TESTE = ("teste", "test", "smoke", "dev")
_ESTADOS_INELEGIVEIS = ("indisponivel", "invalido", "expirado", "stale")


def _destino_real(config) -> bool:
    """Evita que uma regra de homologação seja liberada por engano."""
    grupo_id = str(getattr(config, "grupo_id", "") or "").strip()
    nome = str(getattr(config, "grupo_nome", "") or "").casefold()
    return bool(grupo_id) and not any(marcador in nome for marcador in _MARCADORES_DE_TESTE)


def _taxonomia_catalogo(usuario, *, agora=None) -> dict:
    """Cobertura de macro-categoria do catálogo fresco visível à conta.

    A conta enxerga produtos públicos e seus próprios produtos. É a população
    anterior a qualquer filtro de grupo; medir só o pool já filtrado esconderia
    exatamente os itens sem macro que deixam de virar candidatos.
    """
    from apps.scrapers.models import Produto

    agora = agora or timezone.now()
    itens = (Produto.objects.filter(produtos_frescos_q(agora=agora))
             .filter(Q(owner__isnull=True) | Q(owner=usuario))
             .exclude(estado__in=_ESTADOS_INELEGIVEIS)
             .filter(preco_sem_desconto__gt=0, preco_com_cupom__gt=0))
    total = itens.count()
    classificados = (itens.exclude(macro_categoria__isnull=True)
                     .exclude(macro_categoria="")
                     .exclude(macro_categoria="DESCONHECIDO").count())
    percentual = 0.0 if not total else (classificados * 100.0 / total)
    return {
        "total": total,
        "classificados": classificados,
        "percentual": percentual,
    }


def avaliar(usuario, *, minimo_taxonomia=95.0, agora=None) -> dict:
    """Retorna gates e evidências sem publicar, alterar ou testar segredo."""
    from apps.scrapers import automacao_state
    from apps.scrapers.deal_abundance import relatorio_cobertura
    from apps.scrapers.management.commands.worker_health import ESTEIRAS
    from apps.scrapers.models import ConfiguracaoEnvio

    agora = agora or timezone.now()
    regras = list(ConfiguracaoEnvio.objects.filter(
        owner=usuario, ativo=True, tipo=ConfiguracaoEnvio.TIPO_OFERTAS,
    ).order_by("pk"))
    cobertura = relatorio_cobertura(usuario=usuario, agora=agora)
    por_config = {item["config_id"]: item for item in cobertura["regras"]}
    prioritarias = [
        regra for regra in regras
        if regra.macro_categoria in NICHOS_PRIORITARIOS_LU
    ]
    taxonomia = _taxonomia_catalogo(usuario, agora=agora)
    chat_alerta, emails_alerta = _destinos()
    esteiras = {lane: bool(automacao_state.worker_alive(lane)) for lane in ESTEIRAS}

    gates = {
        "regras_ativas": bool(regras),
        "destinos_reais": bool(regras) and all(_destino_real(regra) for regra in regras),
        "alerta_configurado": bool(chat_alerta or emails_alerta),
        "taxonomia": (
            taxonomia["total"] > 0
            and taxonomia["percentual"] >= float(minimo_taxonomia)
        ),
        "esteiras": bool(esteiras) and all(esteiras.values()),
        # Não basta ter alguma regra: cupom é a prioridade da operação da Lu.
        "nichos_lu_com_cupom": bool(prioritarias) and all(
            bool(por_config.get(regra.pk, {}).get("com_cupom"))
            and not por_config.get(regra.pk, {}).get("deficit_cupom")
            for regra in prioritarias
        ),
        "cobertura_por_regra": bool(cobertura["regras"]) and bool(cobertura["aprovado"]),
    }
    return {
        "aprovado": all(gates.values()),
        "gates": gates,
        "taxonomia": taxonomia,
        "minimo_taxonomia": float(minimo_taxonomia),
        "esteiras": esteiras,
        "config_ids_prioritarios": [regra.pk for regra in prioritarias],
        "cobertura": cobertura,
        "alerta": {"telegram": bool(chat_alerta), "email": bool(emails_alerta)},
    }
