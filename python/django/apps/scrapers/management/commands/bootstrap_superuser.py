"""Cria/atualiza o superusuário a partir de variáveis de ambiente (Fly secrets).

Idempotente: seguro rodar em TODO deploy (via release_command). Lê:
  DJANGO_SUPERUSER_USERNAME   (obrigatório)
  DJANGO_SUPERUSER_PASSWORD   (obrigatório)
  DJANGO_SUPERUSER_EMAIL      (opcional)

Sem username+password, é no-op silencioso (dev não precisa). O env é a fonte da
verdade da conta admin: se a senha no secret mudar, o próximo deploy sincroniza.
Superusuário nasce/vira verificado (passa direto pelo EmailVerificadoMiddleware).
"""
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Cria/atualiza o superusuário a partir de DJANGO_SUPERUSER_* (idempotente)."

    def handle(self, *args, **opts):
        username = os.getenv("DJANGO_SUPERUSER_USERNAME", "").strip()
        email = os.getenv("DJANGO_SUPERUSER_EMAIL", "").strip()
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD", "")
        if not (username and password):
            self.stdout.write("bootstrap_superuser: DJANGO_SUPERUSER_USERNAME/PASSWORD ausentes — ignorando.")
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={"email": email})

        changed = created
        if email and user.email != email:
            user.email = email
            changed = True
        if not (user.is_staff and user.is_superuser):
            user.is_staff = True
            user.is_superuser = True
            changed = True
        # Só reescreve o hash se a senha do secret não bater com a atual (evita write à toa).
        if not user.check_password(password):
            user.set_password(password)
            changed = True
        if changed:
            user.save()

        perfil = getattr(user, "perfil", None)
        if perfil and not perfil.email_verificado:
            perfil.email_verificado = True
            perfil.verificado_em = timezone.now()
            perfil.save(update_fields=["email_verificado", "verificado_em"])

        self.stdout.write(self.style.SUCCESS(
            f"bootstrap_superuser: superusuário '{username}' {'criado' if created else 'sincronizado'}."))
