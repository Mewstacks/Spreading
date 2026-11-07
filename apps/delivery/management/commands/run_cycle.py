"""Um ciclo: ingere feeds, coleta catálogo, enfileira por regra e entrega o que está vencido."""
import logging
import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.coupons.feed import fetch_feed, ingest
from apps.delivery.outbox import deliver_due, enqueue_for_rule
from apps.delivery.transports import SessionRouter
from apps.offers import amazon, amazon_deals, catalog, ml_api, ml_page, shopee
from apps.offers.feeds import fetch_daily_offers, ingest_daily_offers
from apps.offers.models import Rule, SourceRun

logger = logging.getLogger(__name__)


def _summary(value) -> str:
    if isinstance(value, dict):
        return " · ".join(f"{key} {count}" for key, count in value.items() if count)
    return str(value) if value else ""


def _record(label, at, error=None, value=None):
    """Guarda o estado da fonte para a tela Fontes; nunca derruba o ciclo."""
    try:
        run, _created = SourceRun.objects.get_or_create(label=label, defaults={"checked_at": at})
        run.checked_at, run.ok = at, error is None
        if error is not None:
            run.failures, run.error = run.failures + 1, error[:200]
        else:
            run.failures, run.error = 0, ""
            if _summary(value):
                run.result, run.result_at = _summary(value)[:200], at
        run.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("registro da fonte %s falhou: %s", label, exc)


def _safe(label, fn, report, at=None):
    """Uma fonte ruim não derruba o ciclo."""
    at = at or timezone.now()
    try:
        report[label] = fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fonte %s falhou: %s", label, exc)
        report[label] = f"erro: {str(exc)[:80]}"
        _record(label, at, error=str(exc) or type(exc).__name__)
    else:
        _record(label, at, value=report[label])


def run_once(transport=None, now=None, coupon_html=None, offers_html=None) -> dict:
    transport = transport or SessionRouter()
    report, at = {}, now or timezone.now()
    _safe("cupons", lambda: ingest(coupon_html if coupon_html is not None else fetch_feed()),
          report, at)
    _safe("ofertas_dia",
          lambda: ingest_daily_offers(
              offers_html if offers_html is not None else fetch_daily_offers()),
          report, at)
    if ml_api.configured():
        _safe("catalogo", lambda: catalog.collect_catalog(now), report, at)
    _safe("ml_vitrine", lambda: ml_page.collect(now), report, at)
    _safe("amazon", lambda: amazon.collect(now), report, at)
    _safe("amazon_cupons", lambda: amazon_deals.collect(now), report, at)
    if shopee.configured():
        _safe("shopee", lambda: shopee.collect(now), report, at)

    working = None  # None = transporte único: vale a regra antiga de uma sessão só
    if hasattr(transport, "working_sessions"):
        working = transport.working_sessions()
        if not working:
            report["entrega"] = "nenhum número de WhatsApp em WORKING: nada enfileirado nem enviado"
            return report
    elif hasattr(transport, "session_working") and not transport.session_working():
        report["entrega"] = "sessão do WhatsApp fora de WORKING: nada enfileirado nem enviado"
        return report

    queued = 0
    for rule in Rule.unscoped.select_related("org", "destination").filter(active=True):
        if working is not None and rule.destination.session not in working:
            continue  # o número desse grupo está fora do ar: a regra espera
        try:
            if enqueue_for_rule(rule, now):
                queued += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("regra %s falhou: %s", rule.id, exc)
    report["enfileirados"] = queued
    report["entrega"] = deliver_due(transport, now, sessions=working)
    return report


class Command(BaseCommand):
    help = "Ciclo completo (feeds → regras → WhatsApp). Use --loop SEGUNDOS para repetir."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0)

    def handle(self, *args, **options):
        while True:
            try:
                self.stdout.write(str(run_once()))
            except Exception as exc:  # noqa: BLE001  um ciclo ruim não derruba o processo
                self.stderr.write(f"ciclo falhou: {exc}")
            if not options["loop"]:
                return
            time.sleep(options["loop"])
