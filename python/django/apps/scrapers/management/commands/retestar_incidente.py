from django.core.management.base import BaseCommand, CommandError

from apps.scrapers.health_retest import retest_incident_group
from apps.scrapers.models import IncidenteSaude


class Command(BaseCommand):
    help = (
        "Retesta um grupo de incidentes usando a role de sistema auditável; "
        "não é permitido no processo web."
    )

    def add_arguments(self, parser):
        parser.add_argument("incident_id", type=int)

    def handle(self, *args, **options):
        try:
            result = retest_incident_group(options["incident_id"])
        except IncidenteSaude.DoesNotExist as exc:
            raise CommandError("Incidente não encontrado.") from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Reteste concluído: "
                f"{result['completed']} resolvido(s), {result['failed']} pendente(s)."
            )
        )
        if result["failed"]:
            raise CommandError(result["message"] or "Ainda existem falhas no grupo.")
