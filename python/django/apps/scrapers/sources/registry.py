import logging
from contextlib import contextmanager
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from .amazon_public import AmazonPublicSource
from .amazon_coupons import AmazonCouponsSource
from .amazon_general_coupons import AmazonGeneralCouponsSource
from .external_feed import LicensedFeedSource
from .community import PromobitSource as PromobitStub, PelandoSource
from .ml_public_coupons import MLPublicCouponsSource
from .ml_official_promotions import MLOfficialPromotionsSource
from .ml_lightning_coupons import MLLightningCouponsSource
from .meliuz_coupons import MeliuzCouponsSource
from .promobit import PromobitSource
from .shopee import ShopeeCampaignsSource, ShopeeOffersSource
from .shopee_public_coupons import ShopeePublicCouponsSource
from .telegram_publico import TelegramPublicoSource

logger = logging.getLogger(__name__)

SOURCES = {
    AmazonPublicSource.slug: AmazonPublicSource(),
    AmazonCouponsSource.slug: AmazonCouponsSource(),
    AmazonGeneralCouponsSource.slug: AmazonGeneralCouponsSource(),
    LicensedFeedSource.slug: LicensedFeedSource(),
    # Esqueletos históricos permanecem desabilitados; o adaptador real do
    # Promobit é `promobit-cupons` abaixo.
    PromobitStub.slug: PromobitStub(),
    PelandoSource.slug: PelandoSource(),
    MLPublicCouponsSource.slug: MLPublicCouponsSource(),
    MLOfficialPromotionsSource.slug: MLOfficialPromotionsSource(),
    MLLightningCouponsSource.slug: MLLightningCouponsSource(),
    MeliuzCouponsSource.slug: MeliuzCouponsSource(),
    ShopeeOffersSource.slug: ShopeeOffersSource(),
    ShopeeCampaignsSource.slug: ShopeeCampaignsSource(),
    ShopeePublicCouponsSource.slug: ShopeePublicCouponsSource(),
    # Promobit e Telegram são radares de descoberta. A prontidão exige
    # corroboração antes de qualquer publicação; ver coupon_rules.
    PromobitSource.slug: PromobitSource(),
    TelegramPublicoSource.slug: TelegramPublicoSource(),
}


def _public_error(exc):
    text = str(exc).lower()
    if any(x in text for x in ("captcha", "robot", "503", "blocked")):
        return "A fonte bloqueou temporariamente a coleta."
    if "timeout" in text:
        return "A fonte demorou demais para responder."
    return "Falha temporária ao consultar a fonte."


@contextmanager
def _ingestion_guard(slug, *, requires_chromium=False):
    """Cede (adquiriu, motivo). O MOTIVO é o ponto: sem ele, ficar sem navegador
    era registrado como "esta fonte já está executando".

    São dois diagnósticos opostos. `already_running` significa que a fonte está
    trabalhando — não há nada a fazer. `capacity_deferred` significa que ela nem
    começou porque o único slot de Chromium estava com outra tarefa: é fila, e
    aparecia no painel como se a coleta estivesse em curso, escondendo atraso por
    capacidade justamente onde ele precisava ser visto.
    """
    if connection.vendor == "postgresql":
        from apps.scrapers.resource_control import leased_resource
        if requires_chromium:
            # Ordem global em todo o projeto: capacidade antes da sessão/fonte.
            with leased_resource(
                "django_chromium", owner_kind="source_ingest",
            ) as (browser_acquired, _browser_detail):
                if not browser_acquired:
                    yield False, "capacity_deferred"
                    return
                with leased_resource(
                    f"source_ingest:{slug}", owner_kind="source_ingest",
                ) as (source_acquired, _source_detail):
                    yield source_acquired, "" if source_acquired else "already_running"
        else:
            with leased_resource(
                f"source_ingest:{slug}", owner_kind="source_ingest",
            ) as (acquired, _detail):
                yield acquired, "" if acquired else "already_running"
        return
    lock_key = f"ingestion-lock:{slug}"
    acquired = cache.add(lock_key, "1", timeout=20 * 60)
    try:
        yield acquired, "" if acquired else "already_running"
    finally:
        if acquired:
            cache.delete(lock_key)


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
    with _ingestion_guard(
        slug, requires_chromium=bool(getattr(adapter, "requires_chromium", False)),
    ) as (acquired, motivo):
        if not acquired:
            return {
                "status": "running", "offers": [], "coupons": [],
                # `reason_code` separa "já está rodando" de "sem navegador livre".
                # O status continua "running" para não quebrar quem já o lê; quem
                # precisa do diagnóstico correto lê o motivo.
                "reason_code": motivo or "already_running",
            }
        run = ExecucaoIngestao.objects.create(fonte=fonte)
        try:
            offers = list(adapter.discover_offers(**kwargs))
            coupons = list(adapter.discover_coupons(**kwargs))
            total = len(offers) + len(coupons)
            now = timezone.now()
            metrics = dict(getattr(adapter, "last_metrics", {}) or {})
            health = str(
                getattr(adapter, "last_health_status", None)
                or getattr(adapter, "last_health", None)
                or "unknown"
            )[:24]
            with transaction.atomic():
                run.status = "ok" if total else "empty"
                run.total_ofertas, run.total_cupons = len(offers), len(coupons)
                run.finalizada_em = now
                run.metricas = metrics
                run.rejeicoes = metrics.get("rejected_by_reason", metrics.get("rejections", {}))
                run.duracoes = dict(metrics.get("duration_by_stage_ms") or {})
                run.duracoes["total_ms"] = metrics.get("duration_ms", 0)
                run.paginas_processadas = metrics.get("pages_processed", 0)
                run.schema_fingerprint = str(metrics.get("schema_fingerprint") or "")[:64]
                run.health_status = health
                run.save()
                fonte.ultima_tentativa = now
                fonte.ultimo_total = total
                if total and health not in {"blocked", "degraded", "partial"}:
                    fonte.status, fonte.ultimo_sucesso = "ok", now
                    fonte.falhas_consecutivas, fonte.erro_publico = 0, ""
                elif total:
                    # Resultado parcial é útil e será persistido, mas não prova
                    # inventário completo nem autoriza expirar o catálogo anterior.
                    fonte.status = "degraded"
                    fonte.erro_publico = (
                        "Coleta parcial; dados anteriores foram preservados."
                    )
                elif health == "healthy_empty":
                    fonte.status = "ok"
                    fonte.ultimo_sucesso = now
                    fonte.falhas_consecutivas = 0
                    fonte.erro_publico = ""
                else:
                    fonte.status = "degraded"
                    fonte.erro_publico = (
                        "A coleta terminou sem encontrar itens; dados anteriores preservados."
                    )
                fonte.save()
            public_status = (
                "degraded"
                if health in {"blocked", "degraded", "partial"}
                else run.status
            )
            return {
                "status": public_status, "offers": offers, "coupons": coupons,
                "metrics": metrics, "health": health,
            }
        except Exception as exc:
            logger.exception("Fonte %s falhou", slug)
            msg = _public_error(exc)
            now = timezone.now()
            metrics = dict(getattr(adapter, "last_metrics", {}) or {})
            run.status, run.erro_publico, run.finalizada_em = "error", msg, now
            run.metricas = metrics
            run.rejeicoes = metrics.get("rejected_by_reason", metrics.get("rejections", {}))
            run.duracoes = dict(metrics.get("duration_by_stage_ms") or {})
            run.duracoes["total_ms"] = metrics.get("duration_ms", 0)
            run.paginas_processadas = metrics.get("pages_processed", 0)
            run.schema_fingerprint = str(metrics.get("schema_fingerprint") or "")[:64]
            run.health_status = str(
                getattr(adapter, "last_health_status", None)
                or getattr(adapter, "last_health", None)
                or "degraded"
            )[:24]
            run.save(update_fields=[
                "status", "erro_publico", "finalizada_em", "metricas", "rejeicoes",
                "duracoes", "paginas_processadas", "schema_fingerprint",
                "health_status",
            ])
            fonte.ultima_tentativa = now
            fonte.falhas_consecutivas += 1
            fonte.status = "blocked" if fonte.falhas_consecutivas >= 5 else "degraded"
            fonte.erro_publico = msg
            fonte.save()
            return {
                "status": "error", "offers": [], "coupons": [], "error": msg,
                "metrics": metrics,
            }
