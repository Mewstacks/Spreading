"""Um ciclo: ingere feeds, coleta catálogo, enfileira por regra e entrega o que está vencido."""
import logging
import time

from django.core.management.base import BaseCommand

from apps.coupons.feed import fetch_feed, ingest
from apps.delivery.outbox import deliver_due, enqueue_for_rule
from apps.delivery.transports import WahaTransport
from apps.offers import catalog, ml_api, shopee
from apps.offers.feeds import fetch_daily_offers, ingest_daily_offers
from apps.offers.models import Rule

logger = logging.getLogger(__name__)


def _safe(label, fn, report):
    """Uma fonte ruim não derruba o ciclo."""
    try:
        report[label] = fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("fonte %s falhou: %s", label, exc)
        report[label] = f"erro: {str(exc)[:80]}"


def run_once(transport=None, now=None, coupon_html=None, offers_html=None) -> dict:
    transport = transport or WahaTransport()
    report = {}
    _safe("cupons", lambda: ingest(coupon_html if coupon_html is not None else fetch_feed()),
          report)
    _safe("ofertas_dia",
          lambda: ingest_daily_offers(
              offers_html if offers_html is not None else fetch_daily_offers()),
          report)
    if ml_api.configured():
        _safe("catalogo", lambda: catalog.collect_catalog(now), report)
    if shopee.configured():
        _safe("shopee", lambda: shopee.collect(now), report)

    if hasattr(transport, "session_working") and not transport.session_working():
        report["entrega"] = "sessão do WhatsApp fora de WORKING: nada enfileirado nem enviado"
        return report

    queued = 0
    for rule in Rule.unscoped.select_related("org", "destination").filter(active=True):
        try:
            if enqueue_for_rule(rule, now):
                queued += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("regra %s falhou: %s", rule.id, exc)
    report["enfileirados"] = queued
    report["entrega"] = deliver_due(transport, now)
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
