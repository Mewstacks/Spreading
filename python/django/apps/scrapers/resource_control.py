"""Leases PostgreSQL observáveis para recursos que não podem compartilhar sessão/browser."""

import os
import socket
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.accounts.tenant import system_context
from apps.scrapers.models import ExecucaoRaspagem, ResourceLease, WorkerHeartbeat


LEASE_TTL_SECONDS = 90
HEARTBEAT_SECONDS = 15
MANUAL_AGING_SECONDS = 10 * 60

# Um fluxo pode adquirir o slot global e chamar uma rotina interna que protege o
# ponto exato em que abre o navegador. Sem reentrância, a rotina interna enxergava
# o próprio lease como ocupado e o job desistia (ou esperava até o TTL). O mapa é
# local ao contexto de execução: não torna o lease reentrante entre threads,
# processos, organizações ou requests diferentes.
_held_resources = ContextVar("scraper_held_resources", default=None)


def worker_identity(worker_type: str) -> str:
    machine = os.getenv("FLY_MACHINE_ID") or socket.gethostname() or "local"
    return f"{worker_type}:{machine}:{os.getpid()}"[:120]


def pulse_worker(worker_type, *, state="idle", task_type="", details=None, worker_id=None):
    worker_id = worker_id or worker_identity(worker_type)
    WorkerHeartbeat.objects.update_or_create(
        worker_id=worker_id,
        defaults={
            "worker_type": worker_type,
            "state": state,
            "task_type": task_type,
            "heartbeat_at": timezone.now(),
            "details": details or {},
        },
    )
    return worker_id


@contextmanager
def worker_activity(worker_type, worker_id, task_type):
    stop = threading.Event()

    def _pulse():
        while not stop.wait(HEARTBEAT_SECONDS):
            close_old_connections()
            try:
                with system_context():
                    pulse_worker(
                        worker_type, worker_id=worker_id, state="busy",
                        task_type=task_type,
                    )
            finally:
                close_old_connections()

    pulse_worker(worker_type, worker_id=worker_id, state="busy", task_type=task_type)
    thread = threading.Thread(target=_pulse, daemon=True, name=f"worker-{worker_type}")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
        pulse_worker(worker_type, worker_id=worker_id, state="idle", task_type="")


def _lane_field(owner_kind):
    return "manual_waiting_since" if owner_kind == "manual" else "scheduled_waiting_since"


def acquire(resource_key, *, owner_kind="scheduled", organization=None):
    now = timezone.now()
    token = uuid.uuid4().hex
    with transaction.atomic():
        lease, _ = ResourceLease.objects.select_for_update().get_or_create(
            resource_key=resource_key,
        )
        occupied = bool(lease.owner_token and lease.expires_at and lease.expires_at > now)
        # Um heartbeat do lease pode falhar isoladamente enquanto o job continua
        # vivo e mantendo o Chromium aberto. Não entregue o recurso a outro processo
        # se o heartbeat durável da execução manual ainda prova atividade.
        if not occupied and lease.owner_token:
            live_manual = ExecucaoRaspagem.objects.filter(
                status="running", lease_token=lease.owner_token,
                heartbeat_em__gte=now - timedelta(seconds=LEASE_TTL_SECONDS),
            ).exists()
            if live_manual:
                occupied = True
                lease.heartbeat_at = now
                lease.expires_at = now + timedelta(seconds=LEASE_TTL_SECONDS)
                lease.save(update_fields=["heartbeat_at", "expires_at"])
        if occupied:
            field = _lane_field(owner_kind)
            if getattr(lease, field) is None:
                setattr(lease, field, now)
                lease.save(update_fields=[field])
            return None, {
                "resource": resource_key,
                "owner_kind": lease.owner_kind,
                "expires_at": lease.expires_at,
            }

        aged_manual = bool(
            lease.manual_waiting_since
            and (now - lease.manual_waiting_since).total_seconds() >= MANUAL_AGING_SECONDS
        )
        if owner_kind == "manual" and lease.consecutive_manual >= 2 \
                and lease.scheduled_waiting_since:
            if lease.manual_waiting_since is None:
                lease.manual_waiting_since = now
                lease.save(update_fields=["manual_waiting_since"])
            return None, {"resource": resource_key, "owner_kind": "scheduled_fairness"}
        if owner_kind != "manual" and aged_manual:
            if lease.scheduled_waiting_since is None:
                lease.scheduled_waiting_since = now
                lease.save(update_fields=["scheduled_waiting_since"])
            return None, {"resource": resource_key, "owner_kind": "manual_aged"}

        lease.owner_token = token
        lease.owner_kind = owner_kind
        # Aceita a instância ou um UUID já resolvido antes de entrar no event loop
        # síncrono do Playwright. Consultar Organization dentro desse loop levanta
        # SynchronousOnlyOperation; a FK pelo id preserva exatamente o mesmo vínculo
        # sem fazer ORM na região crítica.
        lease.organization_id = (
            getattr(organization, "pk", organization) if organization else None
        )
        lease.acquired_at = now
        lease.heartbeat_at = now
        lease.expires_at = now + timedelta(seconds=LEASE_TTL_SECONDS)
        if owner_kind == "manual":
            lease.consecutive_manual += 1
            lease.manual_waiting_since = None
        else:
            lease.consecutive_manual = 0
            lease.scheduled_waiting_since = None
        lease.save()
    return token, {
        "resource": resource_key, "owner_kind": owner_kind,
        # Token interno: correlaciona job e lease para recuperação, mas nunca é
        # serializado na API/UI.
        "lease_token": token,
    }


def heartbeat(resource_key, token):
    now = timezone.now()
    return ResourceLease.objects.filter(
        resource_key=resource_key, owner_token=token,
    ).update(heartbeat_at=now, expires_at=now + timedelta(seconds=LEASE_TTL_SECONDS)) == 1


def release(resource_key, token):
    with transaction.atomic():
        lease = ResourceLease.objects.select_for_update().filter(
            resource_key=resource_key,
        ).first()
        if not lease or lease.owner_token != token:
            return False
        lease.owner_token = ""
        lease.owner_kind = ""
        lease.organization = None
        lease.acquired_at = None
        lease.heartbeat_at = None
        lease.expires_at = None
        lease.save(update_fields=[
            "owner_token", "owner_kind", "organization", "acquired_at",
            "heartbeat_at", "expires_at",
        ])
        return True


@contextmanager
def leased_resource(resource_key="django_chromium", *, owner_kind="scheduled",
                    organization=None):
    held = _held_resources.get() or {}
    inherited_token = held.get(resource_key)
    if inherited_token:
        yield True, {
            "resource": resource_key,
            "owner_kind": owner_kind,
            "lease_token": inherited_token,
            "reentrant": True,
        }
        return

    token, detail = acquire(
        resource_key, owner_kind=owner_kind, organization=organization,
    )
    if not token:
        yield False, detail
        return

    stop = threading.Event()

    def _pulse():
        while not stop.wait(HEARTBEAT_SECONDS):
            close_old_connections()
            try:
                with system_context():
                    if not heartbeat(resource_key, token):
                        return
            finally:
                close_old_connections()

    thread = threading.Thread(target=_pulse, daemon=True, name=f"lease-{resource_key}")
    thread.start()
    context_token = _held_resources.set({**held, resource_key: token})
    try:
        yield True, detail
    finally:
        _held_resources.reset(context_token)
        stop.set()
        thread.join(timeout=1)
        release(resource_key, token)
