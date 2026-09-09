"""Repara a fronteira pessoal de contas legadas sem tocar em credenciais.

Usuários criados antes de Organization/Perfil existirem podem continuar com dados
legados (sessão, regras e catálogo), mas sem o tenant que autoriza essas relações.
Este comando é deliberadamente idempotente: só cria Perfil, organização pessoal,
membership e conexão WhatsApp quando o Perfil está ausente; nunca substitui uma
organização ou integração já existente.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Perfil, ensure_personal_organization
from apps.accounts.tenant import system_context


class Command(BaseCommand):
    help = "Lista ou repara Perfil/organização ausente de usuários ativos."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="")
        parser.add_argument(
            "--apply", action="store_true",
            help="Efetiva somente a criação idempotente dos perfis ausentes.",
        )

    def handle(self, *args, **options):
        username = str(options.get("username") or "").strip()
        with system_context():
            users = get_user_model().objects.filter(is_active=True).order_by("pk")
            if username:
                users = users.filter(username=username)
                if not users.exists():
                    raise CommandError(f"Usuário {username!r} não encontrado.")
            missing = list(users.filter(perfil__isnull=True))

            for user in missing:
                self.stdout.write(f"PERFIL_AUSENTE\t{user.username}\t{user.pk}")
            if not options.get("apply"):
                self.stdout.write(f"pendentes={len(missing)}")
                return

            repaired = 0
            for user in missing:
                organization = ensure_personal_organization(user)
                Perfil.objects.get_or_create(
                    user=user,
                    defaults={
                        "organization": organization,
                        "active_organization": organization,
                    },
                )
                repaired += 1
                self.stdout.write(
                    f"REPARADO\t{user.username}\t{organization.pk}"
                )
        self.stdout.write(self.style.SUCCESS(f"reparados={repaired}"))
