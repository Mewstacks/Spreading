import hashlib
import hmac

from django.conf import settings
import django.db.models.deletion
from django.db import migrations, models


def _system_context(schema_editor):
    connection = schema_editor.connection
    if connection.vendor != "postgresql" or not settings.TENANT_CONTEXT_SIGNING_KEY:
        return
    signature = hmac.new(
        settings.TENANT_CONTEXT_SIGNING_KEY.encode("utf-8"), b"system:", hashlib.sha256,
    ).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.system_context', 'on', true), "
            "set_config('app.system_signature', %s, true)",
            [signature],
        )


def backfill_coupon_contracts(apps, schema_editor):
    _system_context(schema_editor)
    Coupon = apps.get_model("scrapers", "CupomNormalizado")
    Relation = apps.get_model("scrapers", "ProdutoCupom")
    Preparation = apps.get_model("scrapers", "CupomPreparacao")
    CouponLink = apps.get_model("scrapers", "LinkAfiliadoCupomUsuario")
    Observation = apps.get_model("scrapers", "CupomFonteObservacao")
    ProductLink = apps.get_model("scrapers", "LinkAfiliadoUsuario")
    RelationLink = apps.get_model("scrapers", "LinkAfiliadoProdutoCupomUsuario")
    Config = apps.get_model("scrapers", "ConfiguracaoEnvio")
    Source = apps.get_model("scrapers", "FonteIngestao")
    legacy_ml_source_ids = set(Source.objects.filter(
        slug="mercadolivre-web",
    ).values_list("pk", flat=True))
    authenticated_source, _ = Source.objects.get_or_create(
        slug="mercadolivre-campanhas",
        defaults={"marketplace": "mercadolivre",
                  "nome": "Mercado Livre — campanhas autenticadas"},
    )
    ml_system_organization_id = getattr(settings, "ML_SYSTEM_ORGANIZATION_ID", "") or None

    coupon_updates = []
    authenticated_coupon_ids = []
    activation_by_coupon = {}
    for coupon in Coupon.objects.all().iterator(chunk_size=500):
        rules = coupon.regras if isinstance(coupon.regras, dict) else {}
        raw_mode = str(rules.get("modo_resgate") or "").lower()
        coupon.redemption_mode = (
            "code" if raw_mode == "codigo" or (coupon.codigo and raw_mode != "ativacao")
            else "activation"
        )
        if rules.get("is_mar_aberto"):
            coupon.scope_type = "sitewide"
        elif rules.get("container_url") or rules.get("container_name"):
            coupon.scope_type = "container"
        elif (coupon.evidencia or {}).get("asins") or (coupon.evidencia or {}).get("product_ids"):
            coupon.scope_type = "product"
        elif rules.get("escopo") or rules.get("acao") or coupon.categoria:
            coupon.scope_type = "category"
        else:
            coupon.scope_type = "sitewide" if coupon.redemption_mode == "code" else "product"
        evidence = coupon.evidencia if isinstance(coupon.evidencia, dict) else {}
        is_authenticated_ml = (
            coupon.fonte_id == authenticated_source.pk
            or (coupon.fonte_id in legacy_ml_source_ids
                and evidence.get("association") == "campaign")
        )
        if is_authenticated_ml:
            coupon.fonte_id = authenticated_source.pk
            authenticated_coupon_ids.append(coupon.pk)
        if is_authenticated_ml:
            coupon.organization_id = ml_system_organization_id
            coupon.data_scope = "organization"
            coupon.audience_scope = "organization"
        else:
            coupon.audience_scope = (
                "organization"
                if coupon.data_scope == "organization" or coupon.owner_id else "public"
            )
        external = str(coupon.external_id or "")
        activation_by_coupon[coupon.pk] = (
            external.split(":", 1)[1] if external.startswith("campanha:") else ""
        )
        coupon_updates.append(coupon)
        if len(coupon_updates) >= 500:
            Coupon.objects.bulk_update(
                coupon_updates, ["redemption_mode", "scope_type", "audience_scope",
                                 "organization", "data_scope", "fonte"],
            )
            coupon_updates = []
    if coupon_updates:
        Coupon.objects.bulk_update(
            coupon_updates, ["redemption_mode", "scope_type", "audience_scope",
                             "organization", "data_scope", "fonte"],
        )
    if authenticated_coupon_ids:
        # Preserve corroborating observations from independent sources.  Updating
        # every observation attached to a migrated coupon collapses distinct
        # provenance into the authenticated source and can violate the tenant
        # uniqueness key when both sources observed the same canonical coupon.
        Observation.objects.filter(
            cupom_id__in=authenticated_coupon_ids,
            fonte_id__in=legacy_ml_source_ids,
        ).update(
            fonte_id=authenticated_source.pk,
            organization_id=ml_system_organization_id,
            precedence=30,
        )

    relation_updates = []
    for relation in Relation.objects.all().iterator(chunk_size=1000):
        relation.activation_key = activation_by_coupon.get(relation.cupom_id, "")
        relation_updates.append(relation)
        if len(relation_updates) >= 1000:
            Relation.objects.bulk_update(relation_updates, ["activation_key"])
            relation_updates = []
    if relation_updates:
        Relation.objects.bulk_update(relation_updates, ["activation_key"])

    reason_by_error = {
        "browser_capacity_deferred": "capacity_deferred",
        "container_fetch_failed": "container_fetch_failed",
        "container_empty_proven": "container_empty_proven",
        "minimum_not_met": "minimum_not_met",
        "Mercado Livre exigiu sessão para abrir a lista deste cupom.":
            "ml_session_required_for_preparation",
        "Nenhum produto comprovadamente aplicavel.": "association_missing",
    }
    preparation_updates = []
    for preparation in Preparation.objects.all().iterator(chunk_size=1000):
        preparation.reason_code = reason_by_error.get(
            preparation.erro, "preparation_failed" if preparation.status == "erro" else
            "association_missing" if preparation.status == "vazio" else "",
        )
        preparation.safe_detail = str(preparation.erro or "")[:255]
        preparation_updates.append(preparation)
        if len(preparation_updates) >= 1000:
            Preparation.objects.bulk_update(
                preparation_updates, ["reason_code", "safe_detail"],
            )
            preparation_updates = []
    if preparation_updates:
        Preparation.objects.bulk_update(
            preparation_updates, ["reason_code", "safe_detail"],
        )

    # Links históricos continuam armazenados, mas entram como não verificados.
    # O próximo ciclo os valida sem abrir o Link Builder antes de promovê-los.
    CouponLink.objects.filter(afiliado_ok=True).exclude(link_afiliado="").update(
        estado="pronto", verificado_ok=None, url_canonica="",
        verificacao_motivo="Aguardando reverificação após migração.",
    )

    marketplace_by_coupon = dict(Coupon.objects.values_list("pk", "marketplace"))
    relations_by_product = {}
    for relation in Relation.objects.filter(status="confirmado").iterator(chunk_size=1000):
        relations_by_product.setdefault(relation.produto_id, []).append(relation)
    relation_links = []
    for link in ProductLink.objects.filter(
            verificado_ok=True).exclude(link_afiliado="").iterator(chunk_size=1000):
        for relation in relations_by_product.get(link.produto_id, []):
            activation = str(relation.activation_key or "")
            marketplace = marketplace_by_coupon.get(relation.cupom_id, "")
            if (marketplace == "mercadolivre" and activation
                    and f"coupon_campaign_id={activation}" not in str(link.url_isca or "")):
                continue
            relation_links.append(RelationLink(
                usuario_id=link.usuario_id,
                organization_id=link.organization_id,
                relacao_id=relation.pk,
                url_isca=link.url_isca,
                link_afiliado=link.link_afiliado,
                estado="pronto",
                verificado_ok=True,
                verificado_em=link.verificado_em,
                url_canonica=link.url_canonica or link.link_afiliado,
                verificacao_motivo="",
                tentativas=link.tentativas,
                ultima_tentativa=link.ultima_tentativa,
            ))
            if len(relation_links) >= 1000:
                RelationLink.objects.bulk_create(relation_links, ignore_conflicts=True)
                relation_links = []
    if relation_links:
        RelationLink.objects.bulk_create(relation_links, ignore_conflicts=True)

    Config.objects.filter(
        tipo="aviso_cupons", marketplace="", ativo=True,
    ).update(
        ativo=False,
        motivo_pausa="Escolha Mercado Livre ou Amazon para reativar esta regra.",
    )


def noop_reverse(apps, schema_editor):
    pass


def install_relation_link_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    from apps.accounts.rls import policy_statements

    with schema_editor.connection.cursor() as cursor:
        for statement in policy_statements(
            "scrapers_linkafiliadoprodutocupomusuario",
            mixed=False,
            system_role=settings.TENANT_SYSTEM_DB_ROLE,
            migration_role=settings.TENANT_MIGRATION_DB_ROLE,
        ):
            cursor.execute(statement)


def uninstall_relation_link_rls(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE "scrapers_linkafiliadoprodutocupomusuario" '
            'NO FORCE ROW LEVEL SECURITY'
        )
        cursor.execute(
            'ALTER TABLE "scrapers_linkafiliadoprodutocupomusuario" '
            'DISABLE ROW LEVEL SECURITY'
        )


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0060_publicacao_v2_queue_payload"),
    ]

    operations = [
        migrations.AddField(
            model_name="cupomnormalizado", name="redemption_mode",
            field=models.CharField(
                blank=True, choices=[("", "Legado/indefinido"), ("code", "Código"),
                                     ("activation", "Ativação")],
                db_index=True, default="", max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="cupomnormalizado", name="scope_type",
            field=models.CharField(
                blank=True, choices=[("", "Legado/indefinido"),
                                     ("sitewide", "Site inteiro"),
                                     ("category", "Categoria"),
                                     ("container", "Container"),
                                     ("product", "Produto")],
                db_index=True, default="", max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="cupomnormalizado", name="audience_scope",
            field=models.CharField(
                choices=[("public", "Público"), ("organization", "Organização")],
                db_index=True, default="public", max_length=16,
            ),
        ),
        migrations.AddField(model_name="produtocupom", name="activation_key",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=160)),
        migrations.AddField(model_name="cupompreparacao", name="reason_code",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=64)),
        migrations.AddField(model_name="cupompreparacao", name="safe_detail",
                            field=models.CharField(blank=True, default="", max_length=255)),
        migrations.AddField(model_name="cupompreparacao", name="tentativas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="cupompreparacao", name="duracao_ms",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="cupompreparacao", name="source_run_id",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=80)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="estado",
                            field=models.CharField(
                                choices=[("pendente", "Na fila"), ("pronto", "Link gerado"),
                                         ("nao_afiliavel", "Não afiliável"),
                                         ("erro", "Falhou")],
                                db_index=True, default="pendente", max_length=20)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="verificado_ok",
                            field=models.BooleanField(blank=True, db_index=True,
                                                      default=None, null=True)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="verificado_em",
                            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="url_canonica",
                            field=models.URLField(blank=True, default="", max_length=1500)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="verificacao_motivo",
                            field=models.CharField(blank=True, default="", max_length=300)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="tentativas",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="ultimo_erro",
                            field=models.CharField(blank=True, default="", max_length=300)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="ultima_tentativa",
                            field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="linkafiliadocupomusuario", name="proxima_tentativa",
                            field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="marketplace",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=20)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="source",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=80)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="use_mode",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=24)),
        migrations.AddField(model_name="cupomdisponibilidadeevento",
                            name="evidence_strength",
                            field=models.CharField(blank=True, default="", max_length=32)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="attempt",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="duration_ms",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="cupomdisponibilidadeevento", name="source_run_id",
                            field=models.CharField(blank=True, db_index=True, default="",
                                                   max_length=80)),
        migrations.CreateModel(
            name="LinkAfiliadoProdutoCupomUsuario",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True,
                                            serialize=False, verbose_name="ID")),
                ("url_isca", models.URLField(blank=True, default="", max_length=1000)),
                ("link_afiliado", models.URLField(blank=True, default="", max_length=1500)),
                ("estado", models.CharField(
                    choices=[("pendente", "Na fila"), ("pronto", "Pronto"),
                             ("erro", "Falhou"), ("nao_afiliavel", "Não afiliável")],
                    db_index=True, default="pendente", max_length=20)),
                ("verificado_ok", models.BooleanField(blank=True, db_index=True,
                                                       default=None, null=True)),
                ("verificado_em", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("url_canonica", models.URLField(blank=True, default="", max_length=1500)),
                ("verificacao_motivo", models.CharField(blank=True, default="",
                                                         max_length=300)),
                ("tentativas", models.PositiveIntegerField(default=0)),
                ("ultima_tentativa", models.DateTimeField(blank=True, null=True)),
                ("proxima_tentativa", models.DateTimeField(blank=True, db_index=True,
                                                            null=True)),
                ("organization", models.ForeignKey(
                    blank=True, db_index=True, null=True,
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="links_produto_cupom", to="accounts.organization")),
                ("relacao", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="links_usuarios", to="scrapers.produtocupom")),
                ("usuario", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="links_produto_cupom", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "constraints": [models.UniqueConstraint(
                    fields=("usuario", "relacao"),
                    name="uniq_link_produto_cupom_usuario")],
            },
        ),
    ]
