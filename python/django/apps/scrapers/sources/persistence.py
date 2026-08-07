from django.utils import timezone


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


def persist_items(items, owner=None, integration=None):
    """Idempotent upsert. Empty input deliberately performs no deletion."""
    from apps.scrapers.models import (
        Produto, FonteIngestao, CupomNormalizado, ProgramaAfiliado,
    )
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome
    offers = coupons = 0
    for item in items:
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug=item.source,
            defaults={"marketplace": item.marketplace, "nome": item.source},
        )
        if item.kind == "coupon":
            from apps.scrapers.coupon_rules import derivar_categoria_cupom
            programa = None
            advertiser_id = str((item.evidence or {}).get("advertiser_id") or "")
            if integration and advertiser_id:
                programa = ProgramaAfiliado.objects.filter(
                    integracao=integration, external_id=advertiser_id).first()
            cupom_obj, _ = CupomNormalizado.objects.update_or_create(
                fonte=fonte, external_id=item.external_id, owner=owner,
                defaults={"marketplace": item.marketplace, "titulo": item.title,
                          "codigo": item.coupon_code, "regras": item.coupon_rules,
                          "categoria": derivar_categoria_cupom(item.title, item.coupon_rules),
                          "integracao": integration, "programa": programa,
                          "tipo_conteudo": item.content_type,
                          "anunciante_nome": str((item.evidence or {}).get(
                              "advertiser_name") or "")[:180],
                          "link": item.canonical_url, "validade": item.valid_until,
                          "inicio": item.starts_at, "restrito": item.restricted,
                          "relampago": item.flash,
                          "estado": "ativo", "confianca": "media",
                          "evidencia": item.evidence},
            )
            from apps.scrapers.coupon_products import atualizar_chave_cupom
            atualizar_chave_cupom(cupom_obj)
            coupons += 1
            continue
        lookup = {"marketplace": item.marketplace, "owner": owner}
        lookup["asin" if item.marketplace == "amazon" else "link_produto"] = (
            item.external_id if item.marketplace == "amazon" else item.canonical_url)
        anterior = Produto.objects.filter(**lookup).values_list(
            "evidencia", flat=True).first()
        defaults = {
            "origem": "oferta", "nome": item.title,
            "preco_sem_desconto": item.reference_price,
            "preco_com_cupom": item.current_price,
            "preco_fonte": item.reference_price,
            "preco_efetivo": item.effective_price or item.current_price,
            "link_produto": item.canonical_url, "fonte": item.source,
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
            item.external_id if item.marketplace == "amazon" else "",
            item.canonical_url,
            defaults["preco_efetivo"],
        )
        offers += 1
    return {"offers": offers, "coupons": coupons, "at": timezone.now()}
