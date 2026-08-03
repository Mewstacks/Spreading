from django.core.management.base import BaseCommand, CommandError

from apps.accounts.ml_sessions import load_storage_state_for_organization
from apps.accounts.models import Organization
from apps.accounts.tenant import system_job


class Command(BaseCommand):
    help = (
        "Lista, sem PII, organizações ativas com sessão ML válida para configurar "
        "ML_SYSTEM_ORGANIZATION_ID."
    )

    @system_job
    def handle(self, *args, **options):
        candidatas = []
        for organization in Organization.objects.filter(status="active"):
            try:
                state = load_storage_state_for_organization(organization)
            except Exception:
                state = None
            if state:
                candidatas.append(str(organization.pk))

        if not candidatas:
            raise CommandError("Nenhuma organização ativa possui sessão ML válida.")
        for organization_id in candidatas:
            self.stdout.write(f"ML_SESSION_ORGANIZATION={organization_id}")
        if len(candidatas) == 1:
            self.stdout.write(self.style.SUCCESS(
                "Candidata única; use o UUID acima em ML_SYSTEM_ORGANIZATION_ID."
            ))
        else:
            raise CommandError(
                "Há múltiplas candidatas; escolha explicitamente a organização "
                "operacional aprovada."
            )
