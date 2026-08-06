from django.db import migrations


DIRECT_USER_MODELS = {
    "Produto": "owner_id",
    "IntegracaoAfiliado": "owner_id",
    "CupomNormalizado": "owner_id",
    "CupomPreparacao": "usuario_id",
    "HistoricoEnvio": "usuario_id",
    "Publicacao": "usuario_id",
    "LinkAfiliadoCupomUsuario": "usuario_id",
    "ReceitaAfiliado": "usuario_id",
    "RelatorioSync": "usuario_id",
    "EventoOperacional": "usuario_id",
    "IncidenteSaude": "usuario_id",
    "LinkAfiliadoUsuario": "usuario_id",
    "CanalMonitorado": "owner_id",
    "EnvioCanal": "owner_id",
    "ConfiguracaoEnvio": "owner_id",
}


def backfill(apps, schema_editor):
    Organization = apps.get_model("accounts", "Organization")
    organizations = {
        organization.personal_owner_id: organization.pk
        for organization in Organization.objects.exclude(personal_owner_id=None)
    }

    for model_name, user_field in DIRECT_USER_MODELS.items():
        Model = apps.get_model("scrapers", model_name)
        for row in Model.objects.filter(
            organization_id=None,
        ).exclude(**{user_field: None}).only("pk", user_field).iterator():
            user_id = getattr(row, user_field)
            organization_id = organizations.get(user_id)
            if organization_id is None:
                raise RuntimeError(
                    f"{model_name} {row.pk}: user {user_id} sem organização pessoal"
                )
            updates = {"organization_id": organization_id}
            if model_name in {"Produto", "CupomNormalizado"}:
                updates["data_scope"] = "organization"
            Model.objects.filter(pk=row.pk).update(**updates)

    Programa = apps.get_model("scrapers", "ProgramaAfiliado")
    for row in Programa.objects.filter(organization_id=None).select_related("integracao"):
        Programa.objects.filter(pk=row.pk).update(
            organization_id=row.integracao.organization_id,
        )

    Execucao = apps.get_model("scrapers", "ExecucaoIngestao")
    for row in Execucao.objects.filter(
        organization_id=None, integracao_id__isnull=False,
    ).select_related("integracao"):
        Execucao.objects.filter(pk=row.pk).update(
            organization_id=row.integracao.organization_id,
        )

    Clique = apps.get_model("scrapers", "CliquePublicacao")
    for row in Clique.objects.filter(organization_id=None).select_related("publicacao"):
        Clique.objects.filter(pk=row.pk).update(
            organization_id=row.publicacao.organization_id,
        )

    ProdutoCupom = apps.get_model("scrapers", "ProdutoCupom")
    for row in ProdutoCupom.objects.filter(
        organization_id=None,
    ).select_related("produto", "cupom"):
        product_org = row.produto.organization_id
        coupon_org = row.cupom.organization_id
        if product_org and coupon_org and product_org != coupon_org:
            raise RuntimeError(
                f"ProdutoCupom {row.pk}: relação cross-tenant detectada"
            )
        ProdutoCupom.objects.filter(pk=row.pk).update(
            organization_id=product_org or coupon_org,
        )

    # NULL nestes modelos não é catálogo público; é ambiguidade e bloqueia o release.
    for model_name, user_field in {
        "ConfiguracaoEnvio": "owner_id",
        "HistoricoEnvio": "usuario_id",
    }.items():
        Model = apps.get_model("scrapers", model_name)
        if Model.objects.filter(**{user_field: None}).exists():
            raise RuntimeError(
                f"{model_name}: há linha privada sem owner; corrija antes do deploy"
            )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_backfill_personal_organizations"),
        ("scrapers", "0051_canalmonitorado_organization_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

