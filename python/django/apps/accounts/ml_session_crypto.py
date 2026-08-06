"""Envelope encryption AES-256-GCM para storage state do Mercado Livre."""

import base64
import hashlib
import json
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class MLSessionCryptoError(Exception):
    pass


def _decode_key(value: str) -> bytes:
    try:
        padded = value + ("=" * (-len(value) % 4))
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception as exc:
        raise ImproperlyConfigured("KEK de sessão ML não é base64 válida.") from exc
    if len(key) != 32:
        raise ImproperlyConfigured("Cada KEK de sessão ML deve ter 32 bytes.")
    return key


def keyring() -> dict[str, bytes]:
    raw = settings.ML_SESSION_KEKS_JSON.strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ImproperlyConfigured("ML_SESSION_KEKS_JSON não é JSON válido.") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise ImproperlyConfigured("ML_SESSION_KEKS_JSON deve ser um objeto não vazio.")
        return {str(version): _decode_key(str(value)) for version, value in parsed.items()}

    if settings.APP_ENV in {"development", "test"}:
        # Somente para tornar dev/test reproduzíveis. Produção nunca deriva chave.
        return {
            "dev": hashlib.sha256(
                f"spreading-dev-ml:{settings.SECRET_KEY}".encode()
            ).digest()
        }
    raise ImproperlyConfigured(
        "ML_SESSION_KEKS_JSON é obrigatório para sessões ML em staging/produção."
    )


def current_key_version() -> str:
    keys = keyring()
    requested = settings.ML_SESSION_CURRENT_KEY_VERSION
    if requested in keys:
        return requested
    if settings.APP_ENV in {"development", "test"} and "dev" in keys:
        return "dev"
    raise ImproperlyConfigured(
        "ML_SESSION_CURRENT_KEY_VERSION não existe em ML_SESSION_KEKS_JSON."
    )


def _aad(kind: str, organization_id, connection_id) -> bytes:
    return (
        f"spreading:{kind}:aes-256-gcm-v1:"
        f"{organization_id}:{connection_id}"
    ).encode("utf-8")


def encrypt_storage_state(storage_state: dict, *, organization_id, connection_id) -> dict:
    if not isinstance(storage_state, dict) or not isinstance(
        storage_state.get("cookies"), list
    ):
        raise MLSessionCryptoError("Storage state ML inválido.")

    version = current_key_version()
    kek = keyring()[version]
    dek = AESGCM.generate_key(bit_length=256)
    wrap_nonce = os.urandom(12)
    data_nonce = os.urandom(12)
    plaintext = json.dumps(
        storage_state, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    wrapped_dek = AESGCM(kek).encrypt(
        wrap_nonce, dek, _aad("ml-dek", organization_id, connection_id),
    )
    ciphertext = AESGCM(dek).encrypt(
        data_nonce, plaintext, _aad("ml-session", organization_id, connection_id),
    )
    return {
        "cipher_version": "aes-256-gcm-v1",
        "key_version": version,
        "wrapped_dek": wrapped_dek,
        "wrap_nonce": wrap_nonce,
        "data_nonce": data_nonce,
        "ciphertext": ciphertext,
    }


def decrypt_storage_state(record) -> dict:
    if record.cipher_version != "aes-256-gcm-v1":
        raise MLSessionCryptoError("Versão de cifra de sessão ML não suportada.")
    keys = keyring()
    kek = keys.get(record.key_version)
    if kek is None:
        raise MLSessionCryptoError("Versão de chave da sessão ML indisponível.")
    try:
        dek = AESGCM(kek).decrypt(
            bytes(record.wrap_nonce),
            bytes(record.wrapped_dek),
            _aad("ml-dek", record.organization_id, record.connection_id),
        )
        plaintext = AESGCM(dek).decrypt(
            bytes(record.data_nonce),
            bytes(record.ciphertext),
            _aad("ml-session", record.organization_id, record.connection_id),
        )
        state = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, ValueError, UnicodeDecodeError, TypeError) as exc:
        raise MLSessionCryptoError(
            "Sessão ML não pôde ser autenticada/descriptografada."
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise MLSessionCryptoError("Conteúdo descriptografado da sessão ML é inválido.")
    return state

