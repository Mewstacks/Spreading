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

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            help=(
                "Seleciona a conexão pelo proprietário da organização. "
                "Obrigatório nos canários de uma conta específica."
            ),
        )
        parser.add_argument(
            "--require-ready",
            action="store_true",
            help=(
                "Falha se a sessão selecionada não estiver realmente conectada. "
                "Use depois de login, deploy ou reinício."
            ),
        )

    def handle(self, *args, **options):
        username = str(options.get("username") or "").strip()
        with system_context():
            filtros = {"organization__status": "active"}
            if username:
                filtros["organization__personal_owner__username"] = username
            connection = WhatsAppConnection.objects.filter(**filtros).order_by(
                "created_at"
            ).first()
            if connection is None:
                alvo = f" para o usuário {username!r}" if username else ""
                raise CommandError(
                    f"Nenhuma conexão WhatsApp ativa disponível{alvo}."
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
        conectado = payload.get("conectado") is True
        fase = str(payload.get("fase") or "desconhecida")
        if options.get("require_ready") and (not conectado or fase != "conectado"):
            alvo = f" de {username!r}" if username else ""
            raise CommandError(
                f"Sessão WhatsApp{alvo} não está pronta "
                f"(conectado={conectado}, fase={fase!r})."
            )

        self.stdout.write(self.style.SUCCESS(
            "Probe WhatsApp aprovado"
            + (f" para {username!r}" if username else "")
            + ": rede privada; sem token negado; capacidade Ed25519 "
            + f"tenant/session/action aceita; conectado={conectado}; fase={fase}."
        ))
