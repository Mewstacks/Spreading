"""Leases PostgreSQL observáveis para recursos que não podem compartilhar sessão/browser."""

import os
import socket
import tempfile
import threading
import time
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
_held_machine_resources = ContextVar(
    "scraper_held_machine_resources", default=None,
)


# Quanto tempo um pedido interativo continua valendo depois de anunciado. É um
# teto de segurança, não a espera em si: quem anuncia apaga o marcador ao sair, e
# um processo morto no meio do login não pode calar os workers para sempre.
INTERESSE_INTERATIVO_TTL_S = 120


def _caminho_de_lock(resource_key: str, sufixo: str = "lock") -> str:
    safe_key = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in str(resource_key)
    )[:100]
    lock_dir = os.getenv("SPREADING_RESOURCE_LOCK_DIR", tempfile.gettempdir())
    os.makedirs(lock_dir, exist_ok=True)
    return os.path.join(lock_dir, f"spreading-{safe_key}.{sufixo}")


@contextmanager
def interesse_interativo(resource_key: str = "django_chromium"):
    """Anuncia que uma PESSOA está esperando este recurso agora.

    O flock sozinho não bastava: quem o segura é um lote (40 links a ~5s cada, ou
    uma raspagem inteira), e o login interativo espera 15s e desiste — a tela abria
    e fechava com "a automação está concluindo uma tarefa de navegador" durante os
    minutos inteiros em que a lane de links roda. Nenhum dos dois lados estava
    errado sozinho; faltava o worker saber que havia alguém na fila.

    O marcador é um arquivo no mesmo diretório do lock, então cruza os nove
    processos do Procfile sem depender do banco (a role web não lê a tabela de
    leases) nem de cache (LocMem é por processo).
    """
    caminho = _caminho_de_lock(resource_key, "wanted")
    try:
        with open(caminho, "w") as marcador:
            marcador.write(str(int(time.time())))
    except OSError:
        # Sinalizar é otimização, não pré-requisito: sem o marcador o login volta
        # ao comportamento antigo (espera e, no limite, desiste).
        yield
        return
    try:
        yield
    finally:
        try:
            os.unlink(caminho)
        except OSError:
            pass


def interesse_interativo_pendente(resource_key: str = "django_chromium") -> bool:
    """True enquanto um login interativo espera por este recurso.

    Lotes longos consultam isto ENTRE itens para devolver o navegador. Terminar o
    item corrente e sair é sempre seguro: cada lane é retomada no ciclo seguinte,
    de onde parou.
    """
    try:
        idade = time.time() - os.stat(_caminho_de_lock(resource_key, "wanted")).st_mtime
    except OSError:
        return False
    return idade <= INTERESSE_INTERATIVO_TTL_S


@contextmanager
def machine_resource_slot(resource_key: str, *, wait_seconds: float = 0):
    """Lock de processo para o recurso físico compartilhado nesta Fly Machine.

    O lease PostgreSQL coordena os workers, mas a role web não pode ler a tabela
    system-only de leases. Os logins interativos, portanto, ficavam invisíveis aos
    workers e dois Chromiums podiam disputar a mesma VM. ``flock`` fecha exatamente
    essa lacuna: todos os processos do Procfile compartilham o mesmo /tmp e o lock é
    liberado pelo kernel inclusive após crash/restart.

    ``wait_seconds`` só é usado pela experiência interativa. Workers falham rápido e
    retomam no próximo tick; o login pode esperar brevemente o browser atual fechar,
    sem abrir um segundo processo pesado enquanto isso.
    """
    held = _held_machine_resources.get() or set()
    if resource_key in held:
        yield True
        return

    try:
        import fcntl
    except ImportError:  # pragma: no cover - produção é Linux; fallback para Windows.
        yield True
        return

    handle = open(_caminho_de_lock(resource_key), "a+")
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        if not acquired:
            yield False
            return
        context_token = _held_machine_resources.set({*held, resource_key})
        try:
            yield True
        finally:
            _held_machine_resources.reset(context_token)
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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
