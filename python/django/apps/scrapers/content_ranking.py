"""Ranking unico de produtos, cupons e promocoes por regra de envio."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.scrapers.coupon_rules import regras_do_cupom
from apps.scrapers.maintenance import freshness_points
from apps.scrapers.models import CupomNormalizado, Publicacao


@dataclass
class ContentCandidate:
    kind: str
    obj: object
    score: float
    reasons: list[str]
    commission: float = 0.0


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
        aguarda_corroboracao_oficial, desconto_para_comprador,
    )
    from apps.scrapers.models import CupomDisponibilidade
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
    candidates = []
    for coupon in pool:
        if not _cupom_rankeavel(coupon, config.owner, prontos, ready_ids):
            continue
        if aguarda_corroboracao_oficial(coupon):
            continue
        if coupon.programa and not (
            coupon.programa.habilitado and coupon.programa.status_vinculo == "joined"
            and coupon.programa.link_status == "online"):
            continue
        if coupon.integracao and not (
            coupon.integracao.habilitada and coupon.integracao.status == "conectada"):
            continue
        rules = regras_do_cupom(coupon)
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


def selecionar_conteudo_para_grupo(config, limit=8):
    candidates = _product_candidates(config, limit) + _coupon_candidates(config, limit)
    candidates.sort(key=lambda item: (-item.score, -item.commission,
                                      item.kind, getattr(item.obj, "id", 0)))
    return candidates[:limit]


def previa_melhor_conteudo(config):
    # A tela de configuracao nao pode fazer verificacoes de rede por produto. Mostra
    # a melhor campanha conhecida; produtos sao validados somente no tick de envio.
    candidates = _coupon_candidates(config, limit=1)
    if not candidates:
        return {"tipo": "product", "titulo": "Melhor oferta de produto disponível no envio",
                "score": None, "motivos": ["desconto, urgência e histórico do destino"]}
    candidate = candidates[0]
    return {"tipo": candidate.kind, "titulo": getattr(candidate.obj, "titulo", "")
            or getattr(candidate.obj, "nome", ""), "score": candidate.score,
            "motivos": candidate.reasons}
