from django.utils import timezone


def persist_items(items, owner=None):
    """Idempotent upsert. Empty input deliberately performs no deletion."""
    from apps.scrapers.models import Produto, FonteIngestao, CupomNormalizado
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import classificar_oferta_por_nome
    offers = coupons = 0
    for item in items:
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug=item.source,
            defaults={"marketplace": item.marketplace, "nome": item.source},
        )
        if item.kind == "coupon":
            CupomNormalizado.objects.update_or_create(
                fonte=fonte, external_id=item.external_id,
                defaults={"marketplace": item.marketplace, "titulo": item.title,
                          "codigo": item.coupon_code, "regras": item.coupon_rules,
                          "categoria": (item.coupon_rules.get("escopo") or "").strip()[:100],
                          "link": item.canonical_url, "validade": item.valid_until,
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
