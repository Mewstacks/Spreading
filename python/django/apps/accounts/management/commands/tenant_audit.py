from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import (
    Membership,
    MercadoLivreSession,
    Organization,
    Perfil,
    WhatsAppConnection,
)
from apps.accounts.tenant import system_context
from apps.scrapers import models


DIRECT = (
    (models.Produto, "owner"),
    (models.IntegracaoAfiliado, "owner"),
    (models.CupomNormalizado, "owner"),
    (models.CupomPreparacao, "usuario"),
    (models.HistoricoEnvio, "usuario"),
    (models.Publicacao, "usuario"),
    (models.LinkAfiliadoCupomUsuario, "usuario"),
    (models.ReceitaAfiliado, "usuario"),
    (models.RelatorioSync, "usuario"),
    (models.EventoOperacional, "usuario"),
    (models.IncidenteSaude, "usuario"),
    (models.LinkAfiliadoUsuario, "usuario"),
    (models.CanalMonitorado, "owner"),
    (models.EnvioCanal, "owner"),
    (models.ConfiguracaoEnvio, "owner"),
)

STRICT = (
    models.IntegracaoAfiliado,
    models.HistoricoEnvio,
    models.Publicacao,
    models.LinkAfiliadoCupomUsuario,
    models.ReceitaAfiliado,
    models.RelatorioSync,
    models.LinkAfiliadoUsuario,
    models.CanalMonitorado,
    models.EnvioCanal,
    models.ConfiguracaoEnvio,
)


class Command(BaseCommand):
    help = "Audita backfill, órfãos e relações cross-tenant. Falha se houver risco."

    def add_arguments(self, parser):
        parser.add_argument("--allow-errors", action="store_true")

    def handle(self, *args, **options):
        errors = []
        with system_context():
            personal = {
                str(org.personal_owner_id): str(org.pk)
                for org in Organization.objects.exclude(personal_owner_id=None)
            }
            duplicate_memberships = (
                Membership.objects.filter(is_active=True)
                .values("organization_id", "user_id")
                .order_by()
            )
            # A constraint já impede duplicatas; a contagem deixa a hipótese explícita.
            if duplicate_memberships.count() != len(set(
                (str(row["organization_id"]), row["user_id"])
                for row in duplicate_memberships
            )):
                errors.append("membership ativa duplicada")

            missing_wa = Organization.objects.filter(
                whatsapp_connection__isnull=True,
            ).count()
            if missing_wa:
                errors.append(
                    f"Organization: {missing_wa} tenant(s) sem conexão WhatsApp"
                )
            invalid_ml = MercadoLivreSession.objects.exclude(
                organization__status__in={
                    "active", "suspended", "migration_blocked",
                },
            ).count()
            if invalid_ml:
                errors.append(
                    f"MercadoLivreSession: {invalid_ml} sessão(ões) órfã(s)"
                )
            invalid_wa = WhatsAppConnection.objects.exclude(
                organization__status__in={
                    "active", "suspended", "migration_blocked",
                },
            ).count()
            if invalid_wa:
                errors.append(
                    f"WhatsAppConnection: {invalid_wa} conexão(ões) órfã(s)"
                )
            for profile in Perfil.objects.only(
                "pk", "user_id", "organization_id",
            ).iterator():
                expected = personal.get(str(profile.user_id))
                if (
                    expected is None
                    or str(profile.organization_id) != expected
                ):
                    errors.append(
                        f"Perfil {profile.pk}: user/organization divergente"
                    )

            for Model, user_field in DIRECT:
                for row in Model.objects.exclude(
                    **{f"{user_field}_id": None}
                ).only("pk", "organization_id", f"{user_field}_id").iterator():
                    expected = personal.get(str(getattr(row, f"{user_field}_id")))
                    if expected is None or str(row.organization_id) != expected:
                        errors.append(
                            f"{Model.__name__} {row.pk}: owner/organization divergente"
                        )

            for Model in STRICT:
                count = Model.objects.filter(organization_id=None).count()
                if count:
                    errors.append(f"{Model.__name__}: {count} linha(s) sem organization")

            for Model in (models.Produto, models.CupomNormalizado):
                bad_public = Model.objects.filter(
                    data_scope="public", organization_id__isnull=False,
                ).count()
                bad_private = Model.objects.filter(
                    data_scope="organization", organization_id__isnull=True,
                ).count()
                if bad_public or bad_private:
                    errors.append(
                        f"{Model.__name__}: scope inválido "
                        f"(public={bad_public}, private={bad_private})"
                    )

            for row in models.ProdutoCupom.objects.select_related(
                "produto", "cupom"
            ).iterator():
                product_org = row.produto.organization_id
                coupon_org = row.cupom.organization_id
                if product_org and coupon_org and product_org != coupon_org:
                    errors.append(f"ProdutoCupom {row.pk}: relação cross-tenant")

        if errors:
            for error in errors[:100]:
                self.stderr.write(error)
            if len(errors) > 100:
                self.stderr.write(f"... mais {len(errors) - 100} erro(s)")
            if not options["allow_errors"]:
                raise CommandError(f"Auditoria encontrou {len(errors)} erro(s).")
        self.stdout.write(self.style.SUCCESS(
            f"Auditoria tenant concluída: {len(errors)} erro(s)."
        ))
