"""Campo de modelo que criptografa (Fernet) o valor em repouso, transparente.

Leitores (`perfil.amazon_credential_secret`) continuam recebendo texto puro; o
banco guarda o ciphertext. Valores legados em texto puro seguem legíveis (o
decrypt devolve o próprio valor quando não tem o prefixo 'fernet:').
"""
from django.db import models

from apps.accounts import crypto


class EncryptedCharField(models.CharField):
    """CharField cujo conteúdo é cifrado ao gravar e decifrado ao ler."""

    def from_db_value(self, value, expression, connection):
        return crypto.decrypt(value) if value is not None else value

    def to_python(self, value):
        # Já em texto puro (form/atribuição) — só normaliza legado marcado.
        return crypto.decrypt(value) if isinstance(value, str) else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return crypto.encrypt(value) if value else value
