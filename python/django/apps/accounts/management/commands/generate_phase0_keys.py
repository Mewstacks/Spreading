import base64
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.management.base import BaseCommand


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = (
        "Gera KEK ML, chave de contexto e par Ed25519; não grava secrets."
    )

    def handle(self, *args, **options):
        private = Ed25519PrivateKey.generate()
        private_raw = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public_raw = private.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.stdout.write(
            "ML_SESSION_KEKS_JSON="
            + json.dumps({"v1": b64(AESGCM.generate_key(bit_length=256))})
        )
        self.stdout.write("ML_SESSION_CURRENT_KEY_VERSION=v1")
        self.stdout.write(
            f"TENANT_CONTEXT_SIGNING_KEY={b64(os.urandom(48))}"
        )
        self.stdout.write(f"WA_CAPABILITY_PRIVATE_KEY={b64(private_raw)}")
        self.stdout.write(
            "WA_CAPABILITY_PUBLIC_KEYS_JSON="
            + json.dumps({"wa-ed25519-v1": b64(public_raw)})
        )
        self.stderr.write(
            "Copie diretamente para o cofre de secrets; não salve esta saída em arquivo."
        )
