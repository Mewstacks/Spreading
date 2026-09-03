"""Ranking unico de produtos, cupons e promocoes por regra de envio."""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.scrapers.coupon_rules import cupom_e_lixo, regras_do_cupom
from apps.scrapers.maintenance import freshness_points
from apps.scrapers.models import CupomNormalizado, Publicacao

logger = logging.getLogger(__name__)


@dataclass
class ContentCandidate:
    kind: str
    obj: object
    score: float
    reasons: list[str]
    commission: float = 0.0


def _pontuar_conversao_loja(clicks, conversions) -> float:
    """Boost conservador pela conversao oficial dos ultimos 30 dias.

    Usa o limite inferior de Wilson (95%), nao a taxa crua: 1 venda em 1 clique
    jamais pode vencer um historico de centenas de visitas.
    """
    import math

    try:
        n = max(0, int(clicks or 0))
        successes = max(0, min(n, int(conversions or 0)))
    except (TypeError, ValueError):
        return 0.0
    if not n or not successes:
        return 0.0
    z = 1.96
    rate = successes / n
    denominator = 1 + (z * z / n)
    centre = rate + (z * z / (2 * n))
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * n)) / n)
    lower = max(0.0, (centre - margin) / denominator)
    # Wilson ainda e otimista em 1/1 (limite inferior ~20%). A confianca de
    # amostra impede esse unico evento de valer mais que cem cliques observados.
    sample_confidence = n / (n + 20.0)
    return round(min(10.0, lower * 50.0 * sample_confidence), 2)


def _aplicar_performance_marketplace(user, candidates):
    """Realimenta o ranking com cliques/conversoes dos portais oficiais."""
    marketplaces = {
        str(getattr(candidate.obj, "marketplace", "") or "").lower()
        for candidate in candidates
        if getattr(candidate.obj, "marketplace", None)
    }
    if not user or not marketplaces:
        return
    from apps.scrapers.models import ReceitaAfiliado

    since = timezone.localdate() - timedelta(days=30)
    rows = (
        ReceitaAfiliado.objects.filter(
            usuario=user, marketplace__in=marketplaces, origem="auto",
            granularidade="dia", data__gte=since,
        )
        .values("marketplace")
        .annotate(clicks=Sum("cliques"), conversions=Sum("conversoes"))
    )
    scores = {
        str(row["marketplace"] or "").lower(): _pontuar_conversao_loja(
            row["clicks"], row["conversions"],
        )
        for row in rows
    }
    for candidate in candidates:
        marketplace = str(
            getattr(candidate.obj, "marketplace", "") or ""
        ).lower()
        boost = scores.get(marketplace, 0.0)
        if boost:
            candidate.score = round(candidate.score + boost, 2)
            candidate.reasons.append(
                "boa conversão da loja nos últimos 30 dias"
            )


def _freshness(observed):
    """Mesma janela da vitrine (48h). 72h pontuava cupom já invisível na tela."""
    return freshness_points(observed)


def _pontuar_performance(posts, clicks) -> float:
    return min(10.0, clicks / posts * 5.0) if posts else 0.0


def _performance(user, destination, *, product_id=None, coupon_id=None):
    if not user:
        return 0.0
    query = Publicacao.objects.filter(usuario=user, destino_id=destination, status="enviado")
    query = query.filter(produto_id=product_id) if product_id else query.filter(
        cupom_normalizado_id=coupon_id)
    stats = query.aggregate(posts=Count("id", distinct=True), clicks=Count("cliques"))
    return _pontuar_performance(stats["posts"], stats["clicks"])


def _performance_em_lote(user, destination, campo, ids) -> dict:
    """{id: score} num único GROUP BY, em vez de um aggregate() por item.

    `_performance` era chamada dentro do laço de candidatos, e o pool tem sempre 80
    itens: 80 agregações por regra de envio, multiplicadas pelo número de regras
    ativas, a cada carregamento de /scrapers/config/.
    """
    if not user or not ids:
        return {}
    linhas = (Publicacao.objects
              .filter(usuario=user, destino_id=destination, status="enviado",
                      **{f"{campo}__in": list(ids)})
              .values(campo)
              .annotate(posts=Count("id", distinct=True), clicks=Count("cliques")))
    return {linha[campo]: _pontuar_performance(linha["posts"], linha["clicks"])
            for linha in linhas}


def _cupom_rankeavel(coupon, owner, prontos, ready_ids) -> bool:
    """Pronto de verdade: projeção ready, ou fallback sem depender só do mapa ML."""
    from apps.scrapers.coupon_rules import (
        ativacao_publicavel, codigo_publicavel, cupom_publicavel,
    )
    if ready_ids:
        return coupon.pk in ready_ids
    if coupon.pk in prontos and cupom_publicavel(coupon, usuario=owner):
        return True
    marketplace = str(coupon.marketplace or "").lower()
    if marketplace in {"shopee", "awin"} and ativacao_publicavel(
            coupon, usuario=owner):
        return True
    if marketplace == "amazon":
        tag = str(getattr(getattr(owner, "perfil", None), "afiliado_tag_amazon", "") or "")
        return bool(tag) and (
            codigo_publicavel(coupon) or ativacao_publicavel(coupon, usuario=owner)
        )
    return False


def _product_candidates(config, limit):
    from apps.scrapers.ofertas import selecionar_item_para_grupo

    macros = [config.macro_categoria] if config.macro_categoria else None
    products = selecionar_item_para_grupo(
        macros_selecionadas=macros, limite_envio=limit,
        horas_cooldown=config.horas_cooldown,
        min_desconto_percent=config.min_desconto_percent,
        termo=config.termo_busca, marketplace=config.marketplace or None,
        usuario=config.owner, grupo_id=config.grupo_id,
        # O envio confirma ao vivo somente o candidato escolhido. Validar todo o
        # shortlist aqui fazia oito chamadas externas e repetia a primeira logo
        # depois em enviar_oferta_de_produto.
        verificar=False,
    )
    performance_por_produto = _performance_em_lote(
        config.owner, config.grupo_id, "produto_id", [p.id for p in products])
    candidates = []
    for product in products:
        percent = float(getattr(product, "desconto_percent", 0) or 0)
        value = min(40.0, max(0.0, percent) / 60.0 * 40.0)
        urgency = 20.0 if getattr(product, "relampago", False) else 0.0
        confidence = {"alta": 15.0, "media": 10.0, "baixa": 3.0}.get(
            getattr(product, "confianca", "media"), 3.0)
        fresh = _freshness(getattr(product, "ultima_observacao", None))
        performance = performance_por_produto.get(product.id, 0.0)
        source = 5.0 if getattr(product, "fonte", "") else 2.0
        reasons = [f"{percent:.0f}% de desconto"]
        if urgency:
            reasons.append("oferta relâmpago")
        if fresh >= 7:
            reasons.append("oferta recente")
        if performance:
            reasons.append("bom histórico neste destino")
        candidates.append(ContentCandidate(
            "product", product, round(value + urgency + confidence + fresh + performance + source, 2),
            reasons,
        ))
    return candidates


def _coupon_candidates(config, limit):
    now = timezone.now()
    from apps.scrapers.coupon_rules import cupons_visiveis_q
    from apps.scrapers.maintenance import cupons_frescos_q
    from apps.scrapers.models import CupomDisponibilidade

    query = CupomNormalizado.objects.select_related(
        "fonte", "integracao", "programa").filter(estado="ativo").filter(
        cupons_visiveis_q(config.owner),
        Q(inicio__isnull=True) | Q(inicio__lte=now),
        cupons_frescos_q(agora=now),
    )
    if not config.incluir_restritos:
        query = query.filter(restrito=False)
    selected_programs = list(config.programas.values_list("id", flat=True))
    if selected_programs:
        query = query.filter(Q(programa_id__in=selected_programs) | Q(programa__isnull=True))
    if config.marketplace:
        query = query.filter(marketplace=config.marketplace)
    if config.macro_categoria:
        query = query.filter(Q(categoria=config.macro_categoria)
                             | Q(titulo__icontains=config.macro_categoria))
    if config.termo_busca:
        terms = [term.strip() for term in config.termo_busca.split(",") if term.strip()]
        term_query = Q()
        for term in terms:
            term_query |= Q(titulo__icontains=term) | Q(categoria__icontains=term)
        if term_query:
            query = query.filter(term_query)
    recent_since = now - timedelta(hours=config.horas_cooldown)
    sent_ids = Publicacao.objects.filter(
        usuario=config.owner, destino_id=config.grupo_id,
        cupom_normalizado__isnull=False,
    ).filter(Q(status="enviado", enviada_em__gte=recent_since)
             | Q(status="incerto", criada_em__gte=recent_since)).values_list(
        "cupom_normalizado_id", flat=True)
    query = query.exclude(id__in=sent_ids)

    # Prontidao vem ANTES do recorte dos mais recentes. Em producao a ingestao
    # costuma colocar dezenas de cupons novos na frente da fila enquanto a
    # validacao/preparacao ainda os percorre. Recortar 80 primeiro e intersectar
    # com ``ready`` depois fazia esse backlog ainda pendente expulsar todos os
    # cupons ja comprovados do pool de envio (1.498 publicaveis e 53 prontos para
    # uma conta, mas zero candidato). Quando ainda nao existe nenhuma projecao
    # pronta no escopo, mantemos o fallback legado de ``ids_cupons_prontos``.
    ready_scope = CupomDisponibilidade.objects.filter(
        usuario=config.owner, channel="whatsapp", stage="ready",
        cupom_id__in=query.values("id"),
    )
    if ready_scope.exists():
        query = query.filter(id__in=ready_scope.values("cupom_id"))

    # POOL POR LOJA, e não os 80 mais recentes no geral. As campanhas do Mercado
    # Livre chegam aos milhares e são sempre as mais recentes: uma amostragem global
    # levava um pool inteiro de ML e nenhum cupom da Amazon chegava a ser pontuado —
    # a loja sumia da seleção automática mesmo tendo cupom oficial pronto.
    tamanho = max(80, limit * 10)
    if config.marketplace:
        pool = list(query.order_by("-ultima_observacao")[:tamanho])
    else:
        lojas = list(
            query.values_list("marketplace", flat=True).distinct()
        )
        por_loja = max(1, tamanho // max(1, len(lojas))) if lojas else tamanho
        pool = []
        for loja in lojas:
            pool.extend(
                query.filter(marketplace=loja)
                .order_by("-ultima_observacao")[:por_loja]
            )
    from apps.scrapers.coupon_products import ids_cupons_prontos
    from apps.scrapers.coupon_rules import (
        aguarda_corroboracao_oficial, corroboracoes_oficiais_em_lote,
        desconto_para_comprador,
    )
    prontos = ids_cupons_prontos(config.owner, pool)
    ready_ids = set(
        CupomDisponibilidade.objects.filter(
            usuario=config.owner, channel="whatsapp", stage="ready",
            cupom_id__in=[coupon.id for coupon in pool],
        ).values_list("cupom_id", flat=True)
    )
    performance_por_cupom = _performance_em_lote(
        config.owner, config.grupo_id, "cupom_normalizado_id",
        ready_ids or prontos)
    corroboracoes = corroboracoes_oficiais_em_lote(pool)
    candidates = []
    for coupon in pool:
        if not _cupom_rankeavel(coupon, config.owner, prontos, ready_ids):
            continue
        if aguarda_corroboracao_oficial(coupon, corroboracoes=corroboracoes):
            continue
        if coupon.programa and not (
            coupon.programa.habilitado and coupon.programa.status_vinculo == "joined"
            and coupon.programa.link_status == "online"):
            continue
        if coupon.integracao and not (
            coupon.integracao.habilitada and coupon.integracao.status == "conectada"):
            continue
        rules = regras_do_cupom(coupon)
        # Segunda trava, não redundante: cupons que já ficaram `ready` ANTES
        # deste filtro existir não são reavaliados na hora — o funil só passa
        # por eles de novo no próximo ciclo de manutenção. Sem isto aqui, um
        # cupom lixo pré-existente continuaria sendo escolhido para envio até
        # a próxima varredura.
        if cupom_e_lixo(rules):
            continue
        discount = rules.get("valor_desconto") if desconto_para_comprador(coupon) else None
        kind = rules.get("tipo_desconto")
        if kind == "porcentagem" and discount is not None:
            if discount < config.min_desconto_percent:
                continue
            value = min(40.0, float(discount) / 60.0 * 40.0)
            discount_reason = f"{float(discount):.0f}% de desconto"
        elif not config.incluir_sem_desconto:
            continue
        else:
            value = min(30.0, float(discount or 0) / 4.0) if kind == "fixo" and discount is not None else 0.0
            discount_reason = "campanha ativa" if discount is None else "desconto em reais"
        urgency = 20.0 if coupon.relampago else (
            12.0 if coupon.validade and coupon.validade <= now + timedelta(hours=12) else 0.0)
        confidence = {"alta": 15.0, "media": 10.0, "baixa": 3.0}.get(
            coupon.confianca, 3.0)
        fresh = _freshness(coupon.ultima_observacao)
        performance = performance_por_cupom.get(coupon.id, 0.0)
        source = 5.0 if coupon.fonte.status == "ok" else 2.0
        restricted_penalty = 8.0 if coupon.restrito else 0.0
        commission = float(coupon.programa.comissao_max or 0) if coupon.programa else 0.0
        reasons = [discount_reason]
        if urgency:
            reasons.append("termina em breve" if not coupon.relampago else "oferta relâmpago")
        if coupon.restrito:
            reasons.append("público restrito, condição informada")
        if performance:
            reasons.append("bom histórico neste destino")
        candidates.append(ContentCandidate(
            "coupon", coupon,
            round(value + urgency + confidence + fresh + performance + source - restricted_penalty, 2),
            reasons, commission,
        ))
    return candidates


def _candidatos_legado(config, limit):
    """Ranking anterior à camada Deal: produto e cupom como conteúdos rivais.

    Continua vivo como referência do shadow e como caminho de rollback. A chave
    `item.kind != "coupon"` fazia cupom validado vencer produto por decreto — o
    defeito que a camada Deal existe para corrigir. Não replicar isto lá.
    """
    candidates = _product_candidates(config, limit) + _coupon_candidates(config, limit)
    _aplicar_performance_marketplace(config.owner, candidates)
    candidates.sort(key=lambda item: (item.kind != "coupon", -item.score,
                                      -item.commission, getattr(item.obj, "id", 0)))
    return candidates


def _rotulo(candidate) -> str:
    obj = candidate.obj
    if candidate.kind == "deal":
        return f"deal:{getattr(obj.produto, 'pk', '?')}"
    return f"{candidate.kind}:{getattr(obj, 'pk', '?')}"


def _registrar_shadow(config, legado, deals):
    """Grava a divergência entre o vencedor atual e o da camada Deal.

    Nunca altera o envio. Existe para que a decisão de ligar `DEAL_LAYER_LIVE` numa
    organização seja tomada sobre divergência observada, não sobre expectativa.
    """
    from apps.scrapers.eventos import log_event

    vencedor_legado = _rotulo(legado[0]) if legado else ""
    vencedor_deal = f"deal:{getattr(deals[0].produto, 'pk', '?')}" if deals else ""
    try:
        log_event(
            "selecao", "deal_shadow",
            f"legado={vencedor_legado or 'nenhum'} deal={vencedor_deal or 'nenhum'}",
            usuario=getattr(config, "owner", None),
            contexto={
                "config_id": getattr(config, "pk", None),
                "destino": getattr(config, "grupo_id", ""),
                "vencedor_legado": vencedor_legado,
                "vencedor_deal": vencedor_deal,
                "divergiu": bool(vencedor_legado != vencedor_deal),
                "deals_elegiveis": len(deals),
                "legado_elegiveis": len(legado),
                "score_deal": deals[0].score if deals else None,
                "prova": deals[0].prova if deals else "",
                "preco_final": deals[0].preco_final if deals else None,
            },
        )
    except Exception:  # pragma: no cover - telemetria nunca derruba seleção
        pass


def selecionar_conteudo_para_grupo(config, limit=8):
    """Pool ordenado para esta regra. Camada Deal quando ligada, legado quando não.

    O shadow calcula os dois lados e registra a divergência sem trocar o vencedor,
    para que ligar a camada numa organização seja decisão com evidência.
    """
    from django.conf import settings

    from apps.accounts.feature_flags import deal_layer_live_enabled

    live = deal_layer_live_enabled(getattr(config, "owner", None))
    shadow = bool(getattr(settings, "DEAL_LAYER_SHADOW", False))
    deals = []
    if live or shadow:
        try:
            from apps.scrapers.deals import gerar_deals
            deals = gerar_deals(config, limite=limit)
        except Exception:
            logger.exception("Camada Deal falhou; seguindo pelo ranking legado")
            deals = []

    if live and deals:
        candidatos = [
            ContentCandidate("deal", deal, deal.score, list(deal.motivos))
            for deal in deals
        ]
        if shadow:
            _registrar_shadow(config, _candidatos_legado(config, limit), deals)
        return candidatos[:limit]

    legado = _candidatos_legado(config, limit)
    if shadow:
        _registrar_shadow(config, legado, deals)
    if live and not deals and not getattr(
            settings, "DEAL_FALLBACK_CUPOM_SOLTO", True):
        # Organização em modo Deal estrito prefere não publicar a publicar cupom
        # sem produto. Estoque vazio é transitório e resolve no próximo scrape.
        return []
    return legado[:limit]


def previa_melhor_conteudo(config):
    """O que a tela promete tem de ser o que o envio faz.

    A prévia consultava SÓ cupons, sem ordenar pelo score final e sem olhar
    produto: a tela dizia um vencedor e a automação publicava outro. Agora as duas
    chamam o mesmo seletor, em modo leitura.
    """
    candidates = selecionar_conteudo_para_grupo(config, limit=1)
    if not candidates:
        return {"tipo": "product", "titulo": "Melhor oferta disponível no envio",
                "score": None, "motivos": ["desconto, urgência e histórico do destino"]}
    candidate = candidates[0]
    obj = candidate.obj
    if candidate.kind == "deal":
        titulo = getattr(obj.produto, "nome_llm", "") or getattr(
            obj.produto, "nome", "")
    else:
        titulo = getattr(obj, "titulo", "") or getattr(obj, "nome", "")
    return {"tipo": candidate.kind, "titulo": titulo, "score": candidate.score,
            "motivos": candidate.reasons}
