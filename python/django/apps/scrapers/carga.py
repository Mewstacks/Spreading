"""Coordenação observável dos recursos de browser compartilhados."""
import time
from contextlib import contextmanager

from django.db import connections
from django.conf import settings
from apps.accounts.tenant import in_system_context
from apps.scrapers.resource_control import (
    leased_resource, limpar_interesse_de_esteira, machine_resource_slot,
    sinalizar_interesse_de_esteira,
)


class BrowserResourceUnavailable(RuntimeError):
    """O navegador não abriu porque outro job possui o recurso necessário."""


def _registrar_na_fila(resource_key, owner_kind, conseguiu):
    """Põe a esteira na fila do navegador, ou a tira de lá quando ela entra.

    Perder a vez precisa deixar rastro. Antes disto, uma esteira negada só escrevia
    uma linha de log e sumia até o próximo ciclo — quem estava com o Chromium não
    tinha como saber que havia alguém esperando, e a varredura de 40 páginas seguia
    até o fim enquanto links, verificação e envio ficavam parados (medido em
    20/08/2026: `0 enviada(s)` por tick, 9004 projeções paradas).

    Chamado no ponto da NEGATIVA e no da CONQUISTA, então ninguém precisa lembrar de
    limpar depois: quem entrou não está mais na fila.
    """
    if resource_key != "django_chromium":
        # Só o slot global representa a CPU/memória disputada por todas as esteiras.
        # Lease de sessão é exclusão mútua por credencial, não fila de recurso.
        return
    if conseguiu:
        limpar_interesse_de_esteira(owner_kind, resource_key)
    else:
        sinalizar_interesse_de_esteira(owner_kind, resource_key)


@contextmanager
def operacao_pesada(*, resource_key="django_chromium", owner_kind="scheduled",
                    organization=None):
    """Cede ``True`` e registra proprietário/TTL do recurso compartilhado."""
    connection = connections["default"]
    # SQLite local não coordena processos e threads de heartbeat sobre :memory:
    # não compartilham a mesma base. Mantém o caminho determinístico dos testes.
    if connection.vendor != "postgresql":
        yield True
        return
    # ResourceLease tem FORCE RLS e pertence ao control plane. Uma request web
    # não pode abrir Chromium por fora da fila promovendo a própria conexão para
    # o papel de worker; além de furar a capacidade global, isso faria a thread
    # HTTP aguardar uma navegação longa. Os workers instalam system_context antes
    # de chegar aqui. Falhar fechado evita tanto o bypass quanto um erro de RLS
    # opaco para o chamador.
    if not in_system_context():
        yield False
        return
    with leased_resource(
        resource_key, owner_kind=owner_kind, organization=organization,
    ) as (acquired, _detail):
        if not acquired:
            _registrar_na_fila(resource_key, owner_kind, False)
            yield False
            return
        # O lease acima coordena workers via Postgres. O lock local inclui também
        # os logins interativos do processo web, cuja role RLS não pode acessar a
        # tabela de leases. Só o slot global representa CPU/memória da VM; locks de
        # sessão continuam exclusivamente no banco.
        if resource_key != "django_chromium":
            yield True
            return
        with machine_resource_slot(resource_key) as machine_acquired:
            _registrar_na_fila(resource_key, owner_kind, machine_acquired)
            yield machine_acquired


@contextmanager
def browser_resource(*, session_key=None, owner_kind="scheduled", organization=None):
    """Adquire os leases de browser em ordem única e livre de deadlock.

    O slot global limita CPU/memória. O lease opcional de sessão impede que dois
    processos usem simultaneamente o mesmo storage state persistido, sem bloquear
    HTTP, parsing ou operações de banco que não abrem Chromium.
    """
    with operacao_pesada(
        resource_key="django_chromium", owner_kind=owner_kind,
        organization=organization,
    ) as chromium_acquired:
        if not chromium_acquired:
            yield False
            return
        if not session_key:
            yield True
            return
        with operacao_pesada(
            resource_key=session_key, owner_kind=owner_kind,
            organization=organization,
        ) as session_acquired:
            yield session_acquired


@contextmanager
def ml_site_browser_resource(usuario=None, *, owner_kind="scheduled"):
    """Protege o Chromium e a sessão de site ML da organização correta."""
    from apps.accounts.models import Organization, organization_for_user

    if usuario is not None and getattr(usuario, "is_authenticated", False):
        # Geração de links anota o tenant antes de abrir Playwright. Reaproveitar
        # esse UUID evita uma query ORM dentro do event loop da API sync.
        organization = getattr(usuario, "_spreading_organization_id", None)
        if organization is None:
            organization = organization_for_user(usuario)
    else:
        organization_id = getattr(settings, "ML_SYSTEM_ORGANIZATION_ID", "")
        organization = (
            Organization.objects.filter(pk=organization_id, status="active").first()
            if organization_id else None
        )
    if organization is None:
        # Falha fechada: nunca cair numa chave global ambígua que misture sessões.
        yield False
        return
    organization_id = getattr(organization, "pk", organization)
    with browser_resource(
        session_key=f"ml_site_session:{organization_id}",
        owner_kind=owner_kind,
        organization=organization,
    ) as acquired:
        yield acquired


@contextmanager
def coordinated_ml_browser(*, usuario=None, authenticated=False,
                           owner_kind="scheduled"):
    """Coordena o ponto exato que abrirá Chromium para o Mercado Livre.

    Navegação pública anônima consome somente o slot global. Se um storage state
    será usado, também adquire a sessão da organização: duas tarefas jamais leem
    ou renovam a mesma credencial simultaneamente. O chamador continua responsável
    por abrir/fechar o Playwright dentro deste contexto.
    """
    resource = (
        ml_site_browser_resource(usuario, owner_kind=owner_kind)
        if authenticated
        else browser_resource(owner_kind=owner_kind)
    )
    with resource as acquired:
        if not acquired:
            raise BrowserResourceUnavailable(
                "Capacidade de browser ou sessão Mercado Livre ocupada; "
                "a tarefa será retomada pelo worker."
            )
        yield


@contextmanager
def operacao_pesada_com_espera(timeout_s=150, poll_s=5, aviso=None, *,
                               resource_key="django_chromium",
                               owner_kind="manual", organization=None):
    """Como ``operacao_pesada``, mas ESPERA a vez em vez de desistir na hora.

    É o mesmo lock e o mesmo recurso: o objetivo é justamente disputar com o worker
    de links, não criar uma fila paralela.

    Existe para o clique manual em "Gerar links de afiliado". Sem disputar o lock,
    o botão abria um SEGUNDO Chromium no portal de afiliados enquanto o worker já
    estava lá com a MESMA sessão — e o SSO do Mercado Livre derrubava um dos dois
    para a tela de login. O sintoma era "sessão expirada" numa conta perfeitamente
    conectada.

    ``aviso(segundos_decorridos)`` é chamado a cada tentativa frustrada, para o
    chamador informar a espera (no SSE, um ``print``). Devolve ``False`` no timeout:
    não é erro — o worker pega esses produtos no ciclo dele.
    """
    connection = connections["default"]
    if connection.vendor != "postgresql":
        # Mesmo motivo de operacao_pesada: em SQLite (dev/testes) não há
        # concorrência entre processos e a função do Postgres não existe.
        yield True
        return

    # A tabela de leases globais tem FORCE RLS e é deliberadamente system-only.
    # Um endpoint web não pode se promover para a role de worker só para abrir
    # Chromium; o pedido deve permanecer na fila durável consumida pelos workers.
    # Falhar fechado também evita que uma futura view volte a bloquear uma thread
    # HTTP por minutos ou tente contornar a separação de roles do banco.
    if not in_system_context():
        yield False
        return

    inicio = time.monotonic()
    while True:
        with leased_resource(
            resource_key, owner_kind=owner_kind, organization=organization,
        ) as (acquired, _detail):
            if acquired:
                yield True
                return
        se_passou = time.monotonic() - inicio
        if se_passou >= timeout_s:
            yield False
            return
        if aviso is not None:
            aviso(int(se_passou))
        time.sleep(poll_s)
