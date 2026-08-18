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

# Pedidos de raspagem feitos pelo painel também precisam interromper lotes longos.
# O worker manual renova este marcador enquanto disputa o lease e o remove ANTES
# de executar o próprio job, para que as rotinas internas não cedam para si mesmas.
# Um processo morto deixa no máximo um atraso curto: depois do TTL o marcador deixa
# de valer sem depender de cleanup ou do banco.
INTERESSE_MANUAL_TTL_S = 60


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


def sinalizar_interesse_manual(resource_key: str = "django_chromium") -> bool:
    """Publica/renova a prioridade de uma raspagem iniciada por uma pessoa."""
    caminho = _caminho_de_lock(resource_key, "manual-wanted")
    try:
        with open(caminho, "w") as marcador:
            marcador.write(str(int(time.time())))
        return True
    except OSError:
        # O marcador acelera a cessão cooperativa. O lease PostgreSQL continua
        # sendo a autoridade mesmo quando o filesystem temporário falha.
        return False


def limpar_interesse_manual(resource_key: str = "django_chromium") -> None:
    """Remove a prioridade manual assim que o worker conquistou o recurso."""
    try:
        os.unlink(_caminho_de_lock(resource_key, "manual-wanted"))
    except OSError:
        pass


def interesse_manual_pendente(resource_key: str = "django_chromium") -> bool:
    """True para um pedido manual recentemente renovado por web/worker."""
    try:
        idade = time.time() - os.stat(
            _caminho_de_lock(resource_key, "manual-wanted")
        ).st_mtime
    except OSError:
        return False
    return idade <= INTERESSE_MANUAL_TTL_S


def interesse_interativo_pendente(resource_key: str = "django_chromium") -> bool:
    """True enquanto uma ação humana espera por este recurso.

    Mantém o nome público por compatibilidade, mas inclui tanto login ao vivo quanto
    raspagem sob demanda. Lotes longos consultam isto ENTRE itens/páginas para
    devolver o navegador; cada lane automática retoma no ciclo seguinte.
    """
    if interesse_manual_pendente(resource_key):
        return True
    try:
        idade = time.time() - os.stat(_caminho_de_lock(resource_key, "wanted")).st_mtime
    except OSError:
        return False
    return idade <= INTERESSE_INTERATIVO_TTL_S


try:  # POSIX (produção: Linux na Fly).
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - avaliado por plataforma, não por teste.
    _fcntl = None
try:  # Windows (desenvolvimento).
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - idem.
    _msvcrt = None


def _tentar_travar(handle) -> bool:
    """Tenta o lock exclusivo NÃO bloqueante. True = conseguiu.

    Existe com implementação por plataforma porque o fallback anterior — ``yield
    True`` quando ``fcntl`` não existia — desligava a exclusão mútua inteira no
    Windows. O efeito prático era que a máquina de desenvolvimento nunca reproduzia
    contenção de navegador, que é justamente a causa raiz dos incidentes de
    produção: dois Chromiums subiam felizes localmente e o mesmo código enfileirava
    (ou estourava) na Fly. Validar local só vale se local puder falhar igual.
    """
    if _fcntl is not None:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False
    if _msvcrt is not None:
        # msvcrt trava FAIXA DE BYTES a partir da posição atual, não o arquivo:
        # sem o seek, um handle aberto em "a+" travaria o fim do arquivo, que é um
        # offset diferente por processo — ou seja, ninguém disputaria nada.
        handle.seek(0)
        try:
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    # Plataforma sem nenhum dos dois: falha FECHADA. Conceder o slot aqui seria
    # repetir o bug que este módulo existe para evitar.
    return False


def _destravar(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        return
    if _msvcrt is not None:
        handle.seek(0)
        try:
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:  # já liberado pelo fim do processo
            pass


@contextmanager
def machine_resource_slot(resource_key: str, *, wait_seconds: float = 0):
    """Lock de processo para o recurso físico compartilhado nesta Fly Machine.

    O lease PostgreSQL coordena os workers, mas a role web não pode ler a tabela
    system-only de leases. Os logins interativos, portanto, ficavam invisíveis aos
    workers e dois Chromiums podiam disputar a mesma VM. O lock de arquivo fecha
    exatamente essa lacuna: todos os processos do Procfile compartilham o mesmo
    diretório temporário e o lock é liberado pelo sistema inclusive após crash.

    ``wait_seconds`` só é usado pela experiência interativa. Workers falham rápido e
    retomam no próximo tick; o login pode esperar brevemente o browser atual fechar,
    sem abrir um segundo processo pesado enquanto isso.
    """
    held = _held_machine_resources.get() or set()
    if resource_key in held:
        yield True
        return

    handle = open(_caminho_de_lock(resource_key), "a+")
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    acquired = False
    try:
        while True:
            if _tentar_travar(handle):
                acquired = True
                break
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
            _destravar(handle)
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

        # Uma ação explícita no painel não deve perder a corrida para outro ciclo
        # automático no pequeno intervalo entre o holder anterior fechar o browser
        # e o worker manual repetir a aquisição. A fila durável é a fonte de verdade:
        # se o marcador de arquivo expirar ou o processo web morrer, a prioridade
        # continua somente enquanto existir de fato um job queued.
        manual_job_queued = (
            resource_key == "django_chromium"
            and ExecucaoRaspagem.objects.filter(status="queued").exists()
        )
        if owner_kind != "manual" and manual_job_queued:
            if lease.scheduled_waiting_since is None:
                lease.scheduled_waiting_since = now
                lease.save(update_fields=["scheduled_waiting_since"])
            return None, {
                "resource": resource_key,
                "owner_kind": "manual_queued",
            }

        aged_manual = bool(
            lease.manual_waiting_since
            and (now - lease.manual_waiting_since).total_seconds() >= MANUAL_AGING_SECONDS
        )
        if owner_kind == "manual" and not manual_job_queued \
                and lease.consecutive_manual >= 2 and lease.scheduled_waiting_since:
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
