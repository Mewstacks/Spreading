"""Checa conexões (WhatsApp + ML) e dispara e-mails de alerta. Uso manual/cron:
    python manage.py monitorar
Também roda automaticamente a cada tick do loop de envio (automacao --modo envio).
"""
from django.core.management.base import BaseCommand

from apps.scrapers.monitor_conexao import verificar_e_notificar


class Command(BaseCommand):
    help = "Verifica conexões e envia alertas de queda/retomada por e-mail."

    def handle(self, *args, **opts):
        r = verificar_e_notificar()
        self.stdout.write(
            f"Checados {r['checados']} perfil(s); {r['alertas_enviados']} alerta(s) enviado(s)."
        )
