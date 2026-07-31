from urllib.parse import quote

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import WhatsAppConnection
from apps.accounts.tenant import system_context
from apps.accounts.wa_capabilities import issue_capability


class Command(BaseCommand):
    help = (
        "Testa, sem iniciar sessão, a capacidade Ed25519 entre Django e WA privado."
    )

    def handle(self, *args, **options):
        with system_context():
            connection = (
                WhatsAppConnection.objects.filter(
                    organization__status="active",
                )
                .order_by("created_at")
                .first()
            )
            if connection is None:
                raise CommandError(
                    "Nenhuma conexão WhatsApp ativa disponível para o probe."
                )
            token = issue_capability(connection.instance_id, ["status"])
            instance_id = connection.instance_id

        endpoint = (
            settings.WHATSAPP_API_URL.rstrip("/")
            + "/api/status/"
            + quote(instance_id, safe="")
        )
        try:
            denied = requests.get(endpoint, timeout=5)
            accepted = requests.get(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=5,
            )
        except requests.RequestException as exc:
            raise CommandError(
                "Serviço WhatsApp privado indisponível para o probe."
            ) from exc

        if denied.status_code not in {401, 403}:
            raise CommandError(
                "Rota WhatsApp aceitou request sem capacidade."
            )
        if accepted.status_code != 200:
            raise CommandError(
                f"Capacidade Ed25519 foi recusada (HTTP {accepted.status_code})."
            )
        try:
            payload = accepted.json()
        except ValueError as exc:
            raise CommandError("Resposta WhatsApp não é JSON válido.") from exc
        if str(payload.get("instancia")) != str(instance_id):
            raise CommandError("Resposta WhatsApp não corresponde à sessão vinculada.")

        self.stdout.write(self.style.SUCCESS(
            "Probe WhatsApp aprovado: rede privada; sem token negado; capacidade "
            "Ed25519 tenant/session/action aceita."
        ))
