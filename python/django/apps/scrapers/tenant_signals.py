"""Preenche e valida a organização em toda escrita de dados privados."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.signals import pre_save
from django.dispatch import receiver

from apps.accounts.models import organization_for_user
from apps.accounts.tenant import current_organization_id, in_system_context

from . import models


_USER_FIELD_MODELS = {
    models.Produto: "owner",
    models.IntegracaoAfiliado: "owner",
    models.CupomNormalizado: "owner",
    models.CupomPreparacao: "usuario",
    models.HistoricoEnvio: "usuario",
    models.Publicacao: "usuario",
    models.LinkAfiliadoCupomUsuario: "usuario",
    models.ReceitaAfiliado: "usuario",
    models.RelatorioSync: "usuario",
    models.EventoOperacional: "usuario",
    models.IncidenteSaude: "usuario",
    models.LinkAfiliadoUsuario: "usuario",
    models.CanalMonitorado: "owner",
    models.EnvioCanal: "owner",
    models.ConfiguracaoEnvio: "owner",
}


def _validate_current_scope(instance):
    current = current_organization_id()
    target = getattr(instance, "organization_id", None)
    if current and target and str(target) != str(current) and not in_system_context():
        raise PermissionDenied("Escrita cross-tenant bloqueada.")


@receiver(pre_save)
def assign_organization(sender, instance, **kwargs):
    user_field = _USER_FIELD_MODELS.get(sender)
    if user_field:
        user = getattr(instance, user_field, None)
        if user is not None:
            organization = organization_for_user(user)
            if organization is None:
                raise ValidationError("Usuário sem organização ativa.")
            if instance.organization_id and instance.organization_id != organization.pk:
                raise ValidationError("Owner e organização não correspondem.")
            instance.organization = organization

        if sender in {models.Produto, models.CupomNormalizado}:
            instance.data_scope = "organization" if user is not None else "public"

        _validate_current_scope(instance)
        return

    if sender in {models.ProgramaAfiliado, models.ExecucaoIngestao}:
        integration = getattr(instance, "integracao", None)
        if integration is not None:
            instance.organization_id = integration.organization_id
        _validate_current_scope(instance)
        return

    if sender is models.CliquePublicacao:
        instance.organization_id = instance.publicacao.organization_id
        _validate_current_scope(instance)
        return

    if sender is models.ProdutoCupom:
        product_org = instance.produto.organization_id
        coupon_org = instance.cupom.organization_id
        if product_org and coupon_org and product_org != coupon_org:
            raise ValidationError("Produto e cupom pertencem a organizações diferentes.")
        instance.organization_id = product_org or coupon_org
        _validate_current_scope(instance)

