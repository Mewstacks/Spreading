from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.delivery.health import canary_chats, run_canary, run_probe
from apps.delivery.transports import SessionRouter


class Command(BaseCommand):
    help = (
        "Canário de cada número de WhatsApp em uso: com chat de canário (CANARY_CHAT_ID, "
        "CANARY_CHATS) manda uma mensagem e confere o ACK; sem chat, só pergunta ao WhatsApp. "
        "Sai com 1 se algum número falhar."
    )

    def handle(self, *args, **options):
        router, chats = SessionRouter(), canary_chats()
        names = sorted(router.used_sessions() | set(chats) | {settings.WAHA_SESSION})
        failed = False
        for name in names:
            transport = router.for_session(name)
            chat = chats.get(name, "")
            result = run_canary(transport, chat) if chat else run_probe(transport)
            if result["ok"]:
                detail = f"ok ack={result['ack']}" if "ack" in result else "ok (sondagem)"
                self.stdout.write(f"{name}: {detail}")
            else:
                failed = True
                self.stdout.write(f"{name}: FALHOU {result['reason']}")
        if failed:
            raise CommandError("canário falhou")
