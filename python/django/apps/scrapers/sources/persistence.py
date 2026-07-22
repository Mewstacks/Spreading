from django.utils import timezone


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
            CupomNormalizado.objects.update_or_create(
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
            coupons += 1
            continue
        lookup = {"marketplace": item.marketplace, "owner": owner}
        lookup["asin" if item.marketplace == "amazon" else "link_produto"] = (
            item.external_id if item.marketplace == "amazon" else item.canonical_url)
        Produto.objects.update_or_create(
            **lookup,
            defaults={"origem": "oferta", "nome": item.title,
                      "preco_sem_desconto": item.reference_price,
                      "preco_com_cupom": item.current_price,
                      "preco_fonte": item.reference_price, "preco_efetivo": item.current_price,
                      "link_produto": item.canonical_url, "fonte": item.source,
                      "estado": "ativo", "confianca": "media", "evidencia": item.evidence,
                      "valido_ate": item.valid_until, "falha_verificacao": "",
                      "falhas_consecutivas": 0, "categoria": "DESCONHECIDO",
                      "macro_categoria": classificar_oferta_por_nome(item.title)},
        )
        offers += 1
    return {"offers": offers, "coupons": coupons, "at": timezone.now()}
