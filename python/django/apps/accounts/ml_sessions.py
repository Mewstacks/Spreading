"""Repositório único de sessões ML; nunca escolhe sessão de outro tenant."""

import json
import logging
import os
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .ml_session_crypto import (
    MLSessionCryptoError,
    decrypt_storage_state,
    encrypt_storage_state,
)
from .models import MercadoLivreSession, organization_for_user


logger = logging.getLogger(__name__)


def legacy_path(user) -> str:
    if user is None or not getattr(user, "pk", None):
        return ""
    directory = getattr(settings, "ML_AUTH_DIR", "")
    return os.path.join(directory, f"auth_{user.pk}.json") if directory else ""


def load_storage_state(user, *, allow_legacy=None) -> dict | None:
    organization = organization_for_user(user)
    if organization is None:
        return None
    record = MercadoLivreSession.objects.filter(organization=organization).first()
    if record is not None:
        try:
            state = decrypt_storage_state(record)
        except MLSessionCryptoError:
            MercadoLivreSession.objects.filter(pk=record.pk).update(
                status="decrypt_error",
            )
            raise
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            last_used_at=timezone.now(),
        )
        return state

    if allow_legacy is None:
        allow_legacy = settings.ML_LEGACY_SESSION_READ_ENABLED
    path = legacy_path(user)
    if not allow_legacy or not path or not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as handle:
        state = json.load(handle)
    save_storage_state(user, state)
    try:
        os.remove(path)
    except OSError:
        logger.warning("Sessão ML legada migrada, mas o plaintext não pôde ser removido.")
    logger.info("Sessão ML legada migrada para armazenamento cifrado (user=%s).", user.pk)
    return state


@transaction.atomic
def save_storage_state(user, storage_state: dict) -> MercadoLivreSession:
    organization = organization_for_user(user)
    if organization is None:
        raise MLSessionCryptoError("Usuário sem organização ativa.")
    existing = (
        MercadoLivreSession.objects.select_for_update()
        .filter(organization=organization)
        .first()
    )
    connection_id = existing.connection_id if existing else uuid.uuid4()
    encrypted = encrypt_storage_state(
        storage_state,
        organization_id=organization.pk,
        connection_id=connection_id,
    )
    if existing is None:
        return MercadoLivreSession.objects.create(
            organization=organization,
            connection_id=connection_id,
            status="active",
            rotated_at=timezone.now(),
            **encrypted,
        )
    for field, value in encrypted.items():
        setattr(existing, field, value)
    existing.status = "active"
    existing.rotated_at = timezone.now()
    existing.save(update_fields=[
        *encrypted.keys(), "status", "rotated_at", "updated_at",
    ])
    return existing


def delete_storage_state(user) -> bool:
    organization = organization_for_user(user)
    if organization is None:
        return False
    deleted, _ = MercadoLivreSession.objects.filter(
        organization=organization,
    ).delete()
    path = legacy_path(user)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            logger.warning("Falha ao remover sessão ML legada de user=%s.", user.pk)
    return bool(deleted)


def has_storage_state(user) -> bool:
    organization = organization_for_user(user)
    if organization is None:
        return False
    if MercadoLivreSession.objects.filter(
        organization=organization, status="active",
    ).exists():
        return True
    return bool(
        settings.ML_LEGACY_SESSION_READ_ENABLED
        and legacy_path(user)
        and os.path.isfile(legacy_path(user))
    )

