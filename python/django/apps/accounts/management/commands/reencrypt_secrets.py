"""Rotaciona a chave Fernet dos segredos por usuário (Perfil.amazon_credential_secret).

Rode com a chave ANTIGA ainda ativa em SECRETS_FERNET_KEY (o campo decifra em
memória com ela) e passe a chave NOVA por --nova. O comando grava o ciphertext
re-cifrado direto (o prefixo 'fernet:' faz o get_prep_value passar sem re-cifrar).

Passo a passo seguro:
  1. Gere a nova chave:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  2. Com a chave ANTIGA ainda em SECRETS_FERNET_KEY, rode:
       python manage.py reencrypt_secrets --nova <CHAVE_NOVA>
  3. Troque SECRETS_FERNET_KEY p/ a chave NOVA e reinicie os processos.
Use --dry-run p/ só contar sem gravar.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.crypto import _PREFIX
from apps.accounts.models import Perfil


class Command(BaseCommand):
    help = "Re-cifra os segredos dos Perfis da chave Fernet atual p/ uma nova."

    def add_arguments(self, parser):
        parser.add_argument("--nova", required=True, help="Chave Fernet NOVA (base64).")
        parser.add_argument("--dry-run", action="store_true", help="Não grava; só conta.")

    def handle(self, *args, **opts):
        from cryptography.fernet import Fernet
        try:
            nova = Fernet(opts["nova"].encode())
        except Exception as e:
            raise CommandError(f"Chave --nova inválida: {e}")

        dry = opts["dry_run"]
        n_ok = n_skip = 0
        # .exclude(vazio) reduz o conjunto; o acesso ao atributo decifra com a chave ATUAL.
        for perfil in Perfil.objects.exclude(amazon_credential_secret=""):
            plano = perfil.amazon_credential_secret  # decifrado com a chave antiga
            if not plano:
                n_skip += 1
                continue
            novo_cipher = _PREFIX + nova.encrypt(plano.encode()).decode()
            if not dry:
                # update() -> get_prep_value -> encrypt(): já tem prefixo, passa direto.
                Perfil.objects.filter(pk=perfil.pk).update(
                    amazon_credential_secret=novo_cipher)
            n_ok += 1

        verbo = "re-cifraria" if dry else "re-cifrados"
        self.stdout.write(self.style.SUCCESS(
            f"{n_ok} segredo(s) {verbo}, {n_skip} vazio(s)/pulado(s)."))
        if not dry:
            self.stdout.write("Agora troque SECRETS_FERNET_KEY p/ a chave nova e reinicie.")
