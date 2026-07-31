"""Backfill multi-tenant: cria Perfil p/ usuários antigos e atribui as regras/envios
globais (single-tenant) ao primeiro superusuário, p/ nada quebrar pós-migração."""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Perfil = apps.get_model("accounts", "Perfil")
    ConfiguracaoEnvio = apps.get_model("scrapers", "ConfiguracaoEnvio")
    HistoricoEnvio = apps.get_model("scrapers", "HistoricoEnvio")

    # Perfil p/ todo usuário existente (o signal não rodou retroativamente).
    for u in User.objects.all():
        Perfil.objects.get_or_create(
            user=u,
            defaults={"email_verificado": bool(u.is_superuser)},
        )

    # Owner default = 1º superusuário (ou 1º usuário). Sem usuários, não faz nada.
    dono = User.objects.filter(is_superuser=True).order_by("id").first() or \
        User.objects.order_by("id").first()
    if dono:
        ConfiguracaoEnvio.objects.filter(owner__isnull=True).update(owner=dono)
        HistoricoEnvio.objects.filter(usuario__isnull=True).update(usuario=dono)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("scrapers", "0018_configuracaoenvio_owner_historicoenvio_usuario_and_more"),
        ("accounts", "0001_initial"),
    ]

    operations = [migrations.RunPython(backfill, noop)]
