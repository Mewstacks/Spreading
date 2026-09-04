import hashlib
import logging
import re
from datetime import timedelta

from django.utils import timezone

from apps.scrapers.identidade_produto import link_canonico

logger = logging.getLogger(__name__)
_ASIN_EXATO = re.compile(r"^[A-Z0-9]{10}$", re.I)
_ASIN_NA_URL = re.compile(
    r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)|[?&]asin=([A-Z0-9]{10})(?:[&#]|$)",
    re.I,
)


def _amazon_asin(item):
    """Extrai a chave real do produto sem gravar external_id arbitrário em ASIN."""
    external_id = str(getattr(item, "external_id", "") or "").strip().upper()
    if _ASIN_EXATO.fullmatch(external_id):
        return external_id
    match = _ASIN_NA_URL.search(str(getattr(item, "canonical_url", "") or ""))
    if not match:
        return ""
    return str(match.group(1) or match.group(2) or "").upper()


# As únicas lojas que o creator consegue afiliar hoje. Multiloja continua
# valendo como MARKETPLACE DE FONTE (um agregador que cobre as três), mas o
# item persistido precisa dizer a qual delas pertence.
LOJAS_PERMITIDAS = frozenset({"mercadolivre", "amazon", "shopee"})


_SOURCE_PRECEDENCE = {
    "ml-cupons-afiliados": 10,
    "ml-official-promotions": 10,
    "ml-lightning-coupons": 10,
    "mercadolivre-web": 20,
    # A mesma campanha vista também na vitrine pública deve vencer a observação
    # autenticada: é a confirmação que permite ampliar a audiência.
    "mercadolivre-campanhas": 30,
    "ml-public-containers": 30,
    "licensed-affiliate-feed": 40,
    "amazon-public-coupons": 10,
    "amazon-public-web": 10,
    "amazon-general-coupons": 10,
    # Menor é mais forte. Estas duas ficam no fim de propósito: são ALEGAÇÃO de
    # terceiro, não observação nossa. Servem para corroborar o que outra fonte já
    # viu e para achar cupom que nos escapou — não para, sozinhas, mandar um
    # influenciador anunciar um código ao grupo dele.
    "promobit-cupons": 80,
    "meliuz-cupons": 85,
    "pelando-cupons": 88,
    "bia-garimpa-cupons": 86,
    "cupomspot-cupons": 87,
    "prima-ryca-cupons": 89,
    "discoup-cupons": 90,
    "promomia-cupons": 92,
    "cuponation-cupons": 93,
    "cashbe-cupons": 94,
    "peguei-barato-cupons": 95,
    "linkerhub-cupons": 91,
    "telegram-publico": 90,
    "shopee-public-coupons": 10,
}


def _coupon_canonical_key(item):
    rules = item.coupon_rules if isinstance(item.coupon_rules, dict) else {}
    code = str(item.coupon_code or "").strip().upper()
    if code:
        scope = str(rules.get("container_name") or rules.get("escopo") or "site").strip().lower()
        period = f"{rules.get('dia_inicio') or ''}:{rules.get('dia_fim') or ''}"
        raw = f"code:{item.marketplace}:{code}:{scope}:{period}"
    else:
        evidence = item.evidence if isinstance(item.evidence, dict) else {}
        promotion = evidence.get("promotion_id") or item.external_id
        raw = f"activation:{item.marketplace}:{promotion}:{rules.get('container_name') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _model_coupon_canonical_key(coupon):
    rules = coupon.regras if isinstance(coupon.regras, dict) else {}
    code = str(coupon.codigo or "").strip().upper()
    if code:
        scope = str(rules.get("container_name") or rules.get("escopo") or "site").strip().lower()
        period = f"{rules.get('dia_inicio') or ''}:{rules.get('dia_fim') or ''}"
        raw = f"code:{coupon.marketplace}:{code}:{scope}:{period}"
    else:
        evidence = coupon.evidencia if isinstance(coupon.evidencia, dict) else {}
        promotion = evidence.get("promotion_id") or coupon.external_id
        raw = f"activation:{coupon.marketplace}:{promotion}:{rules.get('container_name') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_coupon_observation(coupon, *, source=None, health_status="healthy",
                              outcome="accepted", reason_code="", evidence=None,
                              precedence=None):
    """Registra veredito de uma fonte legada sem copiar HTML ou URLs sensíveis."""
    from apps.scrapers.models import CupomFonteObservacao

    source = source or coupon.fonte
    safe_evidence = evidence if isinstance(evidence, dict) else {}
    return CupomFonteObservacao.objects.update_or_create(
        organization_id=coupon.organization_id,
        fonte=source,
        canonical_key=_model_coupon_canonical_key(coupon),
        source_external_id=str(coupon.external_id or "")[:180],
        defaults={
            "cupom": coupon,
            "precedence": (
                _SOURCE_PRECEDENCE.get(source.slug, 100)
                if precedence is None else int(precedence)
            ),
            "health_status": str(health_status or "unknown")[:24],
            "outcome": str(outcome or "invalid")[:32],
            "reason_code": str(reason_code or "")[:64],
            "evidence": {
                "association": str(safe_evidence.get("association") or "")[:80],
                "product_ids": max(0, int(safe_evidence.get("product_ids") or 0)),
                "public": bool(safe_evidence.get("public")),
            },
            "observed_at": timezone.now(),
        },
    )


def evidencia_com_cupom_preservado(evidencia, anterior):
    """Mantém ``promotion.coupon_confirmed`` que outra coleta já provou.

    Duas fontes escrevem a MESMA linha de Produto (chave marketplace+owner+asin): a
    página oficial de cupons da Amazon, que afirma o cupom com identificador de
    promoção e ASINs, e a Creators API, que só o deduz por regex sobre o rótulo da
    promoção. Como as duas gravam `evidencia` inteira em `defaults`, a passada da
    Creators API sobrescrevia `True` por `False` e a linha 🎟️ da mensagem sumia até
    a próxima passada da página de cupons — intermitência que aparecia para a
    cliente como "às vezes vem cupom, às vezes não".

    O flag só sobe (nenhuma fonte consegue provar a AUSÊNCIA de um cupom clipável);
    quem o derruba é a expiração do próprio produto, não uma coleta paralela.
    """
    if not isinstance(evidencia, dict):
        return evidencia
    if not isinstance(anterior, dict):
        return evidencia
    if not (anterior.get("promotion") or {}).get("coupon_confirmed"):
        return evidencia
    promocao = dict(evidencia.get("promotion") or {})
    if promocao.get("coupon_confirmed"):
        return evidencia
    promocao["coupon_confirmed"] = True
    return {**evidencia, "promotion": promocao}


def _enriquecer_codigos_heuristicos(cupom_oficial):
    """Completa candidatos ML pelo mesmo código sem promover evidência fraca.

    O registro oficial segue vencendo pela precedência da observação. O objetivo
    aqui é tirar candidatos do limbo ``missing_discount`` e deixar explícita a
    proveniência dos termos, sem transformar uma heurística isolada em prova.
    """
    if (cupom_oficial.marketplace != "mercadolivre"
            or cupom_oficial.fonte.slug != "ml-cupons-afiliados"
            or not str(cupom_oficial.codigo or "").strip()):
        return 0
    from apps.scrapers.coupon_rules import (
        classificar_contrato_cupom, normalizar_regras_cupom,
    )
    from apps.scrapers.models import CupomNormalizado

    oficiais = normalizar_regras_cupom(
        cupom_oficial.regras, external_id=cupom_oficial.external_id,
        codigo=cupom_oficial.codigo,
    )
    if not oficiais.get("tipo_desconto") or not oficiais.get("valor_desconto"):
        return 0
    atualizados = 0
    candidatos = CupomNormalizado.objects.filter(
        marketplace="mercadolivre", codigo__iexact=cupom_oficial.codigo,
    ).exclude(pk=cupom_oficial.pk).select_related("fonte")
    for candidato in candidatos:
        regras = normalizar_regras_cupom(
            candidato.regras, external_id=candidato.external_id,
            codigo=candidato.codigo,
        )
        if regras.get("tipo_desconto") and regras.get("valor_desconto"):
            continue
        for campo in (
            "tipo_desconto", "valor_desconto", "valor_minimo", "desconto_maximo",
            "escopo", "dia_inicio", "dia_fim",
        ):
            if regras.get(campo) in (None, "", 0) and oficiais.get(campo) not in (None, ""):
                regras[campo] = oficiais[campo]
        evidencia = dict(candidato.evidencia or {})
        evidencia["enriched_by"] = {
            "source": cupom_oficial.fonte.slug,
            "external_id": cupom_oficial.external_id,
        }
        contrato = classificar_contrato_cupom(
            regras=regras, external_id=candidato.external_id,
            codigo=candidato.codigo, evidencia=evidencia,
            categoria=candidato.categoria, owner=candidato.owner,
            data_scope=candidato.data_scope,
        )
        candidato.regras = regras
        candidato.evidencia = evidencia
        candidato.validade = candidato.validade or cupom_oficial.validade
        candidato.inicio = candidato.inicio or cupom_oficial.inicio
        candidato.restrito = candidato.restrito or cupom_oficial.restrito
        for campo, valor in contrato.items():
            setattr(candidato, campo, valor)
        candidato.save(update_fields=[
            "regras", "evidencia", "validade", "inicio", "restrito",
            "redemption_mode", "scope_type", "audience_scope",
        ])
        atualizados += 1
    return atualizados


def persist_items(items, owner=None, integration=None, source_health="healthy"):
    """Idempotent upsert. Empty input deliberately performs no deletion."""
    from apps.scrapers.models import (
        Produto, FonteIngestao, CupomFonteObservacao, CupomNormalizado,
        ProgramaAfiliado,
    )
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome
    offers = coupons = 0
    for item in items:
        # Loja fora do programa não entra no banco — nem como cupom, nem como
        # produto. Divulgar Casas Bahia, Magalu ou Americanas é trabalho para o
        # creator e comissão para outra pessoa; e um catálogo que guarda o que não
        # pode publicar só serve para poluir seleção, relatório e custo de leitura.
        # AliExpress entra nesta lista no dia em que a afiliação dela existir.
        if str(item.marketplace or "").casefold() not in LOJAS_PERMITIDAS:
            logger.info(
                "Item de %s descartado: loja %r fora do programa de afiliados.",
                item.source, item.marketplace,
            )
            continue
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug=item.source,
            defaults={"marketplace": item.marketplace, "nome": item.source},
        )
        if item.kind == "coupon":
            from apps.scrapers.coupon_rules import (
                classificar_contrato_cupom, codigo_humano,
                derivar_categoria_cupom,
            )
            codigo_bruto = str(item.coupon_code or "").strip()
            codigo_invalido = bool(
                codigo_bruto and not codigo_humano(codigo_bruto)
            )
            programa = None
            advertiser_id = str((item.evidence or {}).get("advertiser_id") or "")
            if integration and advertiser_id:
                programa = ProgramaAfiliado.objects.filter(
                    integracao=integration, external_id=advertiser_id).first()
            categoria = derivar_categoria_cupom(item.title, item.coupon_rules)
            contrato = classificar_contrato_cupom(
                regras=item.coupon_rules, external_id=item.external_id,
                codigo=item.coupon_code, evidencia=item.evidence,
                categoria=categoria, owner=owner,
                data_scope="organization" if owner else "public",
            )
            cupom_obj, _ = CupomNormalizado.objects.update_or_create(
                fonte=fonte, external_id=item.external_id, owner=owner,
                defaults={"marketplace": item.marketplace, "titulo": item.title,
                          "codigo": item.coupon_code, "regras": item.coupon_rules,
                          "categoria": categoria, **contrato,
                          "integracao": integration, "programa": programa,
                          "tipo_conteudo": item.content_type,
                          "anunciante_nome": str((item.evidence or {}).get(
                              "advertiser_name") or "")[:180],
                          "link": item.canonical_url, "validade": item.valid_until,
                          "inicio": item.starts_at, "restrito": item.restricted,
                          "relampago": item.flash,
                          "estado": "invalido" if codigo_invalido else "ativo",
                          "confianca": "baixa" if codigo_invalido else "media",
                          "evidencia": item.evidence},
            )
            # ``auto_now`` marca quando o parser rodou. Para feeds com horario
            # proprio (notadamente Telegram), isso renovava um post velho sempre
            # que o mesmo HTML era baixado. Scrapers ao vivo continuam sem escrita
            # adicional; so corrigimos instantes materialmente anteriores.
            observado_em = item.observed_at or timezone.now()
            if timezone.is_naive(observado_em):
                observado_em = timezone.make_aware(observado_em)
            agora = timezone.now()
            observado_em = min(observado_em, agora)
            if observado_em < agora - timedelta(minutes=1):
                CupomNormalizado.objects.filter(pk=cupom_obj.pk).update(
                    ultima_observacao=observado_em,
                )
                cupom_obj.ultima_observacao = observado_em
            from apps.scrapers.coupon_products import atualizar_chave_cupom
            atualizar_chave_cupom(cupom_obj)
            if not codigo_invalido:
                _enriquecer_codigos_heuristicos(cupom_obj)
            evidence = item.evidence if isinstance(item.evidence, dict) else {}
            CupomFonteObservacao.objects.update_or_create(
                organization_id=cupom_obj.organization_id,
                fonte=fonte,
                canonical_key=_coupon_canonical_key(item),
                source_external_id=str(item.external_id or "")[:180],
                defaults={
                    "cupom": cupom_obj,
                    "precedence": _SOURCE_PRECEDENCE.get(item.source, 100),
                    "health_status": str(source_health or "unknown")[:24],
                    "outcome": "invalid" if codigo_invalido else "accepted",
                    "reason_code": (
                        "invalid_coupon_code" if codigo_invalido else ""
                    ),
                    # Somente prova tipada; nunca HTML, cookies ou query strings.
                    "evidence": {
                        "association": str(evidence.get("association") or "")[:80],
                        "has_promotion_id": bool(evidence.get("promotion_id")),
                        "product_ids": len(evidence.get("asins") or evidence.get("product_ids") or []),
                    },
                    "observed_at": observado_em,
                },
            )
            coupons += 1
            continue
        lookup = {"marketplace": item.marketplace, "owner": owner}
        product_asin = ""
        link_produto = item.canonical_url
        if item.marketplace == "amazon":
            product_asin = _amazon_asin(item)
            if not product_asin:
                logger.warning(
                    "Oferta Amazon descartada sem ASIN verificável (fonte=%s).",
                    str(item.source or "")[:80],
                )
                continue
            lookup["asin"] = product_asin
        else:
            # Canonicaliza ANTES de usar como chave: sem isto, duas lanes
            # gravando o mesmo anúncio com URLs de tracking diferentes
            # (click1/mclics vs. limpa) viram duas linhas de Produto, porque
            # não existe constraint de unicidade que pegasse a divergência.
            link_produto = link_canonico(item.marketplace, item.canonical_url)
            if not link_produto:
                logger.warning(
                    "Oferta descartada sem link canonicalizável (marketplace=%s, fonte=%s).",
                    item.marketplace, str(item.source or "")[:80],
                )
                continue
            lookup["link_produto"] = link_produto
        anterior = Produto.objects.filter(**lookup).values_list(
            "evidencia", flat=True).first()
        defaults = {
            "origem": "oferta", "nome": item.title,
            "preco_sem_desconto": item.reference_price,
            "preco_com_cupom": item.current_price,
            "preco_fonte": item.reference_price,
            "preco_efetivo": item.effective_price or item.current_price,
            "link_produto": link_produto, "fonte": item.source,
            "estado": "ativo", "confianca": "media",
            "evidencia": evidencia_com_cupom_preservado(item.evidence, anterior),
            "valido_ate": item.valid_until, "falha_verificacao": "",
            "falhas_consecutivas": 0,
        }
        # Fontes legadas nem sempre fornecem imagem. Ausência não deve apagar uma
        # foto válida já observada por outra coleta do mesmo produto.
        if item.image_url:
            defaults["imagem_url"] = item.image_url[:1000]

        # Taxonomia: só entra em `defaults` (o caminho de UPDATE) quando ESTA coleta
        # realmente descobriu algo. O mesmo ASIN é reingerido por fontes diferentes
        # sobre a mesma linha (marketplace + owner + asin), e as públicas não sabem a
        # categoria. Gravá-la como constante fazia a coleta pública desfazer o que a
        # Creators API tinha classificado pelo browseNodeInfo: o produto voltava para
        # 'DESCONHECIDO' e sumia do filtro de subcategoria da vitrine, que exclui esse
        # valor. Mesma armadilha na macro, que `classificar_oferta_por_nome` devolve
        # como None quando o título não denuncia nada.
        categoria = (item.category or "").strip()[:100]
        macro = classificar_oferta_por_nome(item.title)
        if categoria:
            defaults["categoria"] = categoria
        if macro:
            defaults["macro_categoria"] = macro
        Produto.objects.update_or_create(
            **lookup,
            defaults=defaults,
            # Na criação a linha precisa nascer com as colunas preenchidas, inclusive
            # quando não há sinal nenhum — 'DESCONHECIDO' é o valor honesto para
            # "ninguém classificou ainda", e é o que o resto do código já espera.
            create_defaults={**defaults, "categoria": categoria or "DESCONHECIDO",
                             "macro_categoria": macro},
        )
        # Sem esta observação as fontes públicas nunca acumulam histórico e ficam
        # de fora do gate anti-desconto-falso (precos.stats exige n >= 3).
        from apps.scrapers import precos
        precos.registrar(
            item.marketplace,
            product_asin,
            link_produto,
            defaults["preco_efetivo"],
        )
        offers += 1
    return {"offers": offers, "coupons": coupons, "at": timezone.now()}
