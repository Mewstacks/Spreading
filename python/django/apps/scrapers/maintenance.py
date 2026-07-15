from datetime import timedelta
from django.utils import timezone


def expire_stale(max_age_hours=48):
    """Expiração gradual; não remove linhas nem histórico."""
    from apps.scrapers.models import Produto, CupomNormalizado, ProdutoCupom
    now = timezone.now()
    cutoff = now - timedelta(hours=max_age_hours)
    stale_products = Produto.objects.filter(
        ultima_observacao__lt=cutoff, estado="ativo"
    ).update(estado="stale", falha_verificacao="Fonte sem confirmar a oferta há 48h")
    expired_coupons = CupomNormalizado.objects.filter(
        validade__lt=now, estado="ativo"
    ).update(estado="expirado")
    ProdutoCupom.objects.filter(cupom__estado="expirado").exclude(
        status="expirado").update(status="expirado")
    return {"products": stale_products, "coupons": expired_coupons}
