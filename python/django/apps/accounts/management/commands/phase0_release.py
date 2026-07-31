from django.core.management import call_command
from django.core.management.base import BaseCommand

from apps.accounts.tenant import system_job


class Command(BaseCommand):
    help = "Release fail-closed: migra, audita tenants e provisiona o superuser."

    @system_job
    def handle(self, *args, **options):
        call_command("check", deploy=True)
        call_command("migrate", interactive=False)
        call_command("tenant_audit")
        from django.conf import settings
        if settings.PHASE0_EXPAND_ONLY:
            self.stdout.write(self.style.WARNING(
                "PHASE0_EXPAND_ONLY ativo: constraints/RLS aguardam o cutover "
                "manual; cadastro e automações permanecem congelados."
            ))
        else:
            call_command("tenant_constraints", ensure=True)
            call_command("tenant_constraints", status=True)
            call_command("tenant_rls", enable=True)
            call_command("tenant_rls", status=True)
        call_command("bootstrap_superuser")
        self.stdout.write(self.style.SUCCESS("Release Fase 0 concluído."))
