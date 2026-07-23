import logging
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .amazon_public import AmazonPublicSource
from .amazon_coupons import AmazonCouponsSource
from .external_feed import LicensedFeedSource
from .community import PromobitSource, PelandoSource
from .ml_public_coupons import MLPublicCouponsSource

logger = logging.getLogger(__name__)

SOURCES = {
    AmazonPublicSource.slug: AmazonPublicSource(),
    AmazonCouponsSource.slug: AmazonCouponsSource(),
    LicensedFeedSource.slug: LicensedFeedSource(),
    PromobitSource.slug: PromobitSource(),
    PelandoSource.slug: PelandoSource(),
    MLPublicCouponsSource.slug: MLPublicCouponsSource(),
}


def _public_error(exc):
    text = str(exc).lower()
    if any(x in text for x in ("captcha", "robot", "503", "blocked")):
        return "A fonte bloqueou temporariamente a coleta."
    if "timeout" in text:
        return "A fonte demorou demais para responder."
    return "Falha temporária ao consultar a fonte."


def run_source(slug, **kwargs):
    """Executa um adaptador isoladamente, com lock e circuit breaker duráveis."""
    from apps.scrapers.models import FonteIngestao, ExecucaoIngestao

    adapter = SOURCES[slug]
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug=adapter.slug,
        defaults={"marketplace": adapter.marketplace, "nome": adapter.name},
    )
    if not fonte.habilitada:
        fonte.status = "disabled"
        fonte.save(update_fields=["status"])
        return {"status": "disabled", "offers": [], "coupons": []}
    if fonte.falhas_consecutivas >= 5 and fonte.ultima_tentativa:
        if fonte.ultima_tentativa >= timezone.now() - timezone.timedelta(minutes=30):
            return {"status": "blocked", "offers": [], "coupons": []}
    lock_key = f"ingestion-lock:{slug}"
    if not cache.add(lock_key, "1", timeout=20 * 60):
        return {"status": "running", "offers": [], "coupons": []}
    run = ExecucaoIngestao.objects.create(fonte=fonte)
    try:
        offers = list(adapter.discover_offers(**kwargs))
        coupons = list(adapter.discover_coupons(**kwargs))
        total = len(offers) + len(coupons)
        now = timezone.now()
        with transaction.atomic():
            run.status = "ok" if total else "empty"
            run.total_ofertas, run.total_cupons = len(offers), len(coupons)
            run.finalizada_em = now
            run.save()
            fonte.ultima_tentativa = now
            fonte.ultimo_total = total
            if total:
                fonte.status, fonte.ultimo_sucesso = "ok", now
                fonte.falhas_consecutivas, fonte.erro_publico = 0, ""
            else:
                fonte.status = "degraded"
                fonte.erro_publico = "A coleta terminou sem encontrar itens; dados anteriores preservados."
            fonte.save()
        return {"status": run.status, "offers": offers, "coupons": coupons}
    except Exception as exc:
        logger.exception("Fonte %s falhou", slug)
        msg = _public_error(exc)
        now = timezone.now()
        run.status, run.erro_publico, run.finalizada_em = "error", msg, now
        run.save(update_fields=["status", "erro_publico", "finalizada_em"])
        fonte.ultima_tentativa = now
        fonte.falhas_consecutivas += 1
        fonte.status = "blocked" if fonte.falhas_consecutivas >= 5 else "degraded"
        fonte.erro_publico = msg
        fonte.save()
        return {"status": "error", "offers": [], "coupons": [], "error": msg}
    finally:
        cache.delete(lock_key)
