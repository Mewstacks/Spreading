"""Um ciclo: ingere o feed, enfileira avisos por destino e entrega o que está vencido."""
import time

from django.core.management.base import BaseCommand

from apps.coupons.feed import fetch_feed, ingest
from apps.delivery.outbox import deliver_due, enqueue_coupon_notice
from apps.delivery.transports import WahaTransport
from apps.orgs.models import Destination


def run_once(stdout, transport=None, html=None):
    total = ingest(html if html is not None else fetch_feed())
    queued = 0
    for destination in Destination.unscoped.select_related("org").filter(
        active=True, kind__in=[Destination.WA_GROUP, Destination.WA_CHANNEL]
    ):
        if enqueue_coupon_notice(destination.org, destination):
            queued += 1
    stats = deliver_due(transport or WahaTransport())
    stdout.write(f"feed={total} enfileirados={queued} entrega={stats}")


class Command(BaseCommand):
    help = "Ciclo completo (feed → aviso → WhatsApp). Use --loop SEGUNDOS para repetir."

    def add_arguments(self, parser):
        parser.add_argument("--loop", type=int, default=0)

    def handle(self, *args, **options):
        while True:
            try:
                run_once(self.stdout)
            except Exception as exc:  # um ciclo ruim não derruba o processo
                self.stderr.write(f"ciclo falhou: {exc}")
            if not options["loop"]:
                return
            time.sleep(options["loop"])
