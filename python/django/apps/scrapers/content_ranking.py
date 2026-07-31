"""Ranking unico de produtos, cupons e promocoes por regra de envio."""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from apps.scrapers.coupon_rules import regras_do_cupom
from apps.scrapers.models import CupomNormalizado, Publicacao


@dataclass
class ContentCandidate:
    kind: str
    obj: object
    score: float
    reasons: list[str]
    commission: float = 0.0


def _freshness(observed):
    if not observed:
        return 0.0
    hours = max(0.0, (timezone.now() - observed).total_seconds() / 3600)
    return max(0.0, 10.0 * (1.0 - min(hours, 72.0) / 72.0))


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


def _product_candidates(config, limit):
    from apps.scrapers.ofertas import selecionar_item_para_grupo

    macros = [config.macro_categoria] if config.macro_categoria else None
    products = selecionar_item_para_grupo(
        macros_selecionadas=macros, limite_envio=limit,
        horas_cooldown=config.horas_cooldown,
        min_desconto_percent=config.min_desconto_percent,
        termo=config.termo_busca, marketplace=config.marketplace or None,
        usuario=config.owner, grupo_id=config.grupo_id,
    )
    performance_por_produto = _performance_em_lote(
        config.owner, config.grupo_id, "produto_id", [p.id for p in products])
    candidates = []
    for product in products:
        percent = float(getattr(product, "desconto_percent", 0) or 0)
        value = min(40.0, max(0.0, percent) / 60.0 * 40.0)
        urgency = 20.0 if getattr(product, "relampago", False) else 0.0
        confidence = {"alta": 15.0, "media": 10.0, "baixa": 3.0}.get(
            getattr(product, "confianca", "media"), 8.0)
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
    query = CupomNormalizado.objects.select_related(
        "fonte", "integracao", "programa").filter(estado="ativo").filter(
        Q(owner__isnull=True) | Q(owner=config.owner),
        Q(inicio__isnull=True) | Q(inicio__lte=now),
        Q(validade__isnull=True) | Q(validade__gte=now),
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

    pool = list(query.order_by("-ultima_observacao")[:max(80, limit * 10)])
    from apps.scrapers.coupon_products import ids_cupons_prontos
    from apps.scrapers.coupon_rules import cupom_publicavel
    prontos = ids_cupons_prontos(config.owner, pool)
    performance_por_cupom = _performance_em_lote(
        config.owner, config.grupo_id, "cupom_normalizado_id", prontos)
    candidates = []
    for coupon in pool:
        if coupon.id not in prontos or not cupom_publicavel(coupon):
            continue
        if coupon.programa and not (
            coupon.programa.habilitado and coupon.programa.status_vinculo == "joined"
            and coupon.programa.link_status == "online"):
            continue
        if coupon.integracao and not (
            coupon.integracao.habilitada and coupon.integracao.status == "conectada"):
            continue
        rules = regras_do_cupom(coupon)
        discount = rules.get("valor_desconto")
        kind = rules.get("tipo_desconto")
        if kind == "porcentagem" and discount is not None:
            if discount < config.min_desconto_percent:
                continue
            value = min(40.0, float(discount) / 60.0 * 40.0)
            discount_reason = f"{float(discount):.0f}% de desconto"
        elif not config.incluir_sem_desconto:
            continue
        else:
            value = min(30.0, float(discount or 0) / 4.0) if kind == "fixo" else 0.0
            discount_reason = "campanha ativa" if discount is None else "desconto em reais"
        urgency = 20.0 if coupon.relampago else (
            12.0 if coupon.validade and coupon.validade <= now + timedelta(hours=12) else 0.0)
        confidence = {"alta": 15.0, "media": 10.0, "baixa": 3.0}.get(
            coupon.confianca, 8.0)
        fresh = _freshness(coupon.ultima_observacao)
        performance = performance_por_cupom.get(coupon.id, 0.0)
        source = 5.0 if coupon.fonte.status == "ok" else 2.0
        restricted_penalty = 5.0 if coupon.restrito else 0.0
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
