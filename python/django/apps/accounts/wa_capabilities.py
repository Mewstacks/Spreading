"""Emissão de capacidades Ed25519 curtas e vinculadas a uma sessão WhatsApp."""

import base64
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, PermissionDenied

from .models import WhatsAppConnection


_dev_private_key = None


def _decode_private_key() -> Ed25519PrivateKey:
    global _dev_private_key
    raw = settings.WA_CAPABILITY_PRIVATE_KEY.strip()
    if not raw:
        if settings.APP_ENV in {"development", "test"}:
            if _dev_private_key is None:
                _dev_private_key = Ed25519PrivateKey.generate()
            return _dev_private_key
        raise ImproperlyConfigured(
            "WA_CAPABILITY_PRIVATE_KEY é obrigatória em staging/produção."
        )
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
        return Ed25519PrivateKey.from_private_bytes(key)
    except Exception as exc:
        raise ImproperlyConfigured(
            "WA_CAPABILITY_PRIVATE_KEY deve ser base64url de 32 bytes."
        ) from exc


def connection_for_session(session_id: str) -> WhatsAppConnection:
    """A conexão WhatsApp desta sessão, lida SEMPRE com o escopo de tenant instalado.

    `accounts_whatsappconnection` está sob RLS. Esta função é chamada de dentro de
    `whatsapp_client._headers`, ou seja: de todo request ao worker, inclusive os que
    partem de um job SSE rodando com o tenant apenas ANOTADO (`segurar_transacao=False`).
    Sem reinstalar o escopo a query volta zero linhas, o PermissionDenied vira
    "não foi possível autorizar a sessão" e o envio acusa desconexão enquanto a tela
    — que roda dentro da request, com escopo — mostra tudo conectado.
    """
    from .tenant import executar_orm_ou_direto

    session_id = str(session_id or "").strip()
    if not session_id:
        raise PermissionDenied("Sessão WhatsApp ausente.")

    def _buscar():
        return WhatsAppConnection.objects.select_related("organization").filter(
            instance_id=session_id,
            organization__status="active",
        ).first()

    connection = executar_orm_ou_direto(_buscar)
    if connection is None:
        raise PermissionDenied("Sessão WhatsApp não pertence a organização ativa.")
    return connection


def issue_capability(session_id: str, actions, *, single_use=False) -> str:
    connection = connection_for_session(session_id)
    now = int(time.time())
    action_list = sorted({str(action) for action in actions if action})
    if not action_list:
        raise ValueError("Ao menos uma ação é obrigatória.")
    payload = {
        "iss": settings.WA_CAPABILITY_ISSUER,
        "aud": settings.WA_CAPABILITY_AUDIENCE,
        "sub": str(connection.organization_id),
        "organization_id": str(connection.organization_id),
        "session_id": connection.instance_id,
        "actions": action_list,
        "iat": now,
        "nbf": now - 2,
        "exp": now + settings.WA_CAPABILITY_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
        "single_use": bool(single_use),
    }
    return jwt.encode(
        payload,
        _decode_private_key(),
        algorithm="EdDSA",
        headers={"kid": settings.WA_CAPABILITY_KEY_ID, "typ": "JWT"},
    )


def public_key_base64url() -> str:
    from cryptography.hazmat.primitives import serialization

    raw = _decode_private_key().public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

