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

# Quantos vereditos "suspeito" seguidos até pedir reconexão ao usuário. A sonda
# roda de um IP de datacenter (Fly), onde o gateway anti-bot do ML responde
# 302→login/403 a requisições perfeitamente autenticadas: uma única suspeita não
# é logout, é ruído. Nem mesmo o terceiro veredito apaga a credencial — ver
# `registrar_veredito`.
PROBE_FALHAS_PARA_DESCONECTAR = 3

# Distância mínima entre duas suspeitas para que a segunda conte. São 9 processos
# (web + 8 workers, ver python/Procfile) sondando em paralelo; sem esta janela uma
# única rajada simultânea valeria pelos três ciclos independentes que a política
# exige, e o usuário seria desconectado em segundos pelo mesmo falso-positivo que
# ela existe para absorver.
PROBE_JANELA_S = 15 * 60

# Status em que a credencial ainda serve para trabalhar. `expired` fica de fora
# (o usuário precisa reconectar), mas a linha CONTINUA no banco: a sonda segue
# rodando sobre ela e um veredito "conectado" a traz de volta sozinha.
STATUS_UTILIZAVEIS = ("active", "suspect")

# Quanto tempo entre dois toques em `last_used_at`. Antes toda leitura escrevia,
# e o poll de 3s da tela de conexão virava um UPDATE a cada 3s por aba aberta.
TOUCH_INTERVALO_S = 10 * 60


def legacy_path(user) -> str:
    if user is None or not getattr(user, "pk", None):
        return ""
    directory = getattr(settings, "ML_AUTH_DIR", "")
    return os.path.join(directory, f"auth_{user.pk}.json") if directory else ""


def load_storage_state_for_organization(organization, *, touch=True) -> dict | None:
    """Sessão cifrada de UMA organização, sem passar por um usuário.

    Existe para o loop de automação (@system_job), que alimenta o catálogo
    compartilhado e não tem usuário na mão — ver settings.ML_SYSTEM_ORGANIZATION_ID.
    Nunca escolhe organização sozinha: quem chama já decidiu qual é.

    `touch=False` para leituras que não são uso da credencial (sondas, telas): elas
    não devem escrever no banco. `last_used_at` responde "quando a sessão foi usada
    de verdade", e uma tela aberta não é uso.
    """
    if organization is None:
        return None
    record = MercadoLivreSession.objects.filter(organization=organization).first()
    if record is None:
        return None
    try:
        state = decrypt_storage_state(record)
    except MLSessionCryptoError:
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            status="decrypt_error",
        )
        raise
    if touch:
        agora = timezone.now()
        anterior = record.last_used_at
        if anterior is None or (agora - anterior).total_seconds() >= TOUCH_INTERVALO_S:
            MercadoLivreSession.objects.filter(pk=record.pk).update(last_used_at=agora)
    return state


def load_storage_state(user, *, allow_legacy=None, touch=True) -> dict | None:
    organization = organization_for_user(user)
    if organization is None:
        return None
    state = load_storage_state_for_organization(organization, touch=touch)
    if state is not None:
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
    # Credencial nova zera o histórico da sonda: as suspeitas acumuladas eram sobre
    # os cookies antigos. Sem isto, reconectar deixava o usuário a uma suspeita de
    # ser "desconectado" outra vez.
    existing.last_probe_at = None
    existing.last_probe_result = ""
    existing.probe_failures = 0
    existing.probe_reason = ""
    # O mesmo vale para o histórico do Link Builder: o login novo passa pelo SSO do
    # portal, então as suspeitas sobre os cookies antigos não valem mais.
    existing.lb_last_probe_at = None
    existing.lb_last_probe_result = ""
    existing.lb_probe_failures = 0
    existing.lb_probe_reason = ""
    existing.save(update_fields=[
        *encrypted.keys(), "status", "rotated_at", "updated_at",
        "last_probe_at", "last_probe_result", "probe_failures", "probe_reason",
        "lb_last_probe_at", "lb_last_probe_result", "lb_probe_failures",
        "lb_probe_reason",
    ])
    return existing


def probe_snapshot(organization) -> dict | None:
    """Veredito compartilhado da sonda, sem decifrar nada.

    É o que as telas e o polling leem. Uma query, sem AES, sem UPDATE — ao
    contrário de `load_storage_state`, que precisa decifrar o storage_state
    inteiro e por isso não pode ficar no caminho de um poll de 3 segundos.
    """
    if organization is None:
        return None
    return MercadoLivreSession.objects.filter(organization=organization).values(
        "status", "last_probe_at", "last_probe_result", "probe_failures",
        "probe_reason",
    ).first()


def registrar_veredito(organization, resultado: str, motivo: str = "") -> dict | None:
    """Persiste o veredito da sonda na própria linha da sessão.

    NUNCA apaga a credencial. Antes, um veredito "expirado" chamava
    `delete_storage_state` e a organização inteira perdia a sessão — mas quem
    produzia esse veredito era um GET a partir do IP de datacenter da Fly, onde o
    anti-bot do ML responde 302→login/403 a requisições autenticadas. O usuário
    conectava, via a tela verde, e um worker o desconectava minutos depois.
    `auxiliar.iniciar_browser` já havia parado de revogar pelo mesmo motivo; esta
    era a última fonte que ainda revogava.

    Agora a política é acumulativa e reversível: `PROBE_FALHAS_PARA_DESCONECTAR`
    suspeitas em ciclos distintos marcam a linha como `expired` (a UI pede
    reconexão), mas os bytes continuam no banco e a sonda segue rodando — um único
    veredito "conectado" traz a sessão de volta sem intervenção.
    """
    if organization is None:
        return None
    record = MercadoLivreSession.objects.filter(organization=organization).first()
    if record is None:
        return None

    agora = timezone.now()
    campos = {
        "last_probe_at": agora,
        "last_probe_result": resultado,
        "probe_reason": (motivo or "")[:200],
    }

    if resultado == "conectado":
        campos["probe_failures"] = 0
        if record.status in {"suspect", "expired"}:
            campos["status"] = "active"
    elif resultado == "suspeito":
        falhas = record.probe_failures
        anterior = record.last_probe_at
        # Conta no máximo uma suspeita por janela — ver PROBE_JANELA_S.
        if (record.last_probe_result != "suspeito" or anterior is None
                or (agora - anterior).total_seconds() >= PROBE_JANELA_S):
            falhas += 1
        campos["probe_failures"] = falhas
        campos["status"] = (
            "expired" if falhas >= PROBE_FALHAS_PARA_DESCONECTAR else "suspect"
        )
    # "inconclusivo" (timeout, 5xx, challenge) não mexe no contador: só registra
    # que a sonda rodou, para o próximo ciclo não sondar de novo em seguida.

    MercadoLivreSession.objects.filter(pk=record.pk).update(**campos)
    snapshot = {
        "status": record.status,
        "last_probe_at": record.last_probe_at,
        "last_probe_result": record.last_probe_result,
        "probe_failures": record.probe_failures,
        "probe_reason": record.probe_reason,
    }
    snapshot.update(campos)
    return snapshot


def linkbuilder_snapshot(organization) -> dict | None:
    """Veredito compartilhado da sonda do Link Builder (portal de afiliados).

    Irmão de `probe_snapshot`, sobre as colunas `lb_*`. Devolve as chaves com os
    nomes SEM prefixo para que `conexoes._estado_do_registro` traduza os dois
    escopos com o mesmo código — a política de acúmulo é idêntica, só a coluna
    muda. `status` vem sempre "" de propósito: ver o comentário do modelo.
    """
    if organization is None:
        return None
    linha = MercadoLivreSession.objects.filter(organization=organization).values(
        "lb_last_probe_at", "lb_last_probe_result", "lb_probe_failures",
        "lb_probe_reason",
    ).first()
    if linha is None:
        return None
    return {
        "status": "",
        "last_probe_at": linha["lb_last_probe_at"],
        "last_probe_result": linha["lb_last_probe_result"],
        "probe_failures": linha["lb_probe_failures"],
        "probe_reason": linha["lb_probe_reason"],
    }


def registrar_veredito_linkbuilder(organization, resultado: str,
                                   motivo: str = "") -> dict | None:
    """Persiste o veredito do Link Builder, sem tocar em `status`.

    Mesma política acumulativa e reversível de `registrar_veredito` — inclusive
    PROBE_JANELA_S, pelo mesmo motivo (9 processos sondando em paralelo) — mas
    isolada nas colunas `lb_*`. Não mexer em `status` é o ponto: o portal de
    afiliados recusar o cookie não pode bloquear a raspagem do site, que usa a
    mesma credencial e continua funcionando.
    """
    if organization is None:
        return None
    record = MercadoLivreSession.objects.filter(organization=organization).first()
    if record is None:
        return None

    agora = timezone.now()
    campos = {
        "lb_last_probe_at": agora,
        "lb_last_probe_result": resultado,
        "lb_probe_reason": (motivo or "")[:200],
    }
    if resultado == "conectado":
        campos["lb_probe_failures"] = 0
    elif resultado == "suspeito":
        falhas = record.lb_probe_failures
        anterior = record.lb_last_probe_at
        if (record.lb_last_probe_result != "suspeito" or anterior is None
                or (agora - anterior).total_seconds() >= PROBE_JANELA_S):
            falhas += 1
        campos["lb_probe_failures"] = falhas
    # "inconclusivo" (anti-bot, intersticial, 5xx) não mexe no contador.

    MercadoLivreSession.objects.filter(pk=record.pk).update(**campos)
    return {
        "status": "",
        "last_probe_at": campos["lb_last_probe_at"],
        "last_probe_result": campos["lb_last_probe_result"],
        "probe_failures": campos.get("lb_probe_failures", record.lb_probe_failures),
        "probe_reason": campos["lb_probe_reason"],
    }


def registrar_veredito_linkbuilder_para_usuario(user, resultado: str,
                                               motivo: str = "") -> dict | None:
    """Atalho para quem tem usuário na mão (o próprio Link Builder, em link.py)."""
    return registrar_veredito_linkbuilder(
        organization_for_user(user) if user is not None else None, resultado, motivo,
    )


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
    """A organização tem credencial utilizável AGORA?

    Aceita `suspect`: uma suspeita isolada é ruído do anti-bot, e recusar trabalho
    por causa dela reproduziria — pelo lado do gate, em vez do lado da revogação —
    o mesmo falso-positivo que `registrar_veredito` existe para absorver. Recusa
    `expired` (suspeitas repetidas: o usuário precisa reconectar) e `decrypt_error`,
    que `load_storage_state` também recusa. Antes os dois divergiam sobre a mesma
    linha: este exigia `active` e aquele ignorava `status`.
    """
    organization = organization_for_user(user)
    if organization is None:
        return False
    if MercadoLivreSession.objects.filter(
        organization=organization, status__in=STATUS_UTILIZAVEIS,
    ).exists():
        return True
    return bool(
        settings.ML_LEGACY_SESSION_READ_ENABLED
        and legacy_path(user)
        and os.path.isfile(legacy_path(user))
    )

