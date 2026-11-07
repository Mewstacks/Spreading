import os

from django.core.management.base import BaseCommand, CommandError

from apps.delivery.health import run_canary
from apps.delivery.transports import WahaTransport


class Command(BaseCommand):
    help = "Canário: manda uma mensagem a um grupo privado e confere o ACK (sai com 1 se falhar)."

    def handle(self, *args, **options):
        result = run_canary(WahaTransport(), os.environ.get("CANARY_CHAT_ID", ""))
        self.stdout.write(str(result))
        if not result["ok"]:
            raise CommandError("canário falhou")
