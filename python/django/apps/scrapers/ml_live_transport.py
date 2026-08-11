"""Transporte confiável do navegador remoto usado pelas conexões do Mercado Livre.

O login continua acontecendo num Chromium isolado no servidor, mas o transporte não
depende mais do ``Page.startScreencast`` do CDP. Capturas sob demanda são mais baratas,
mais nítidas e, principalmente, não ficam presas quando um ACK de frame se perde.

Este módulo não sabe qual URL representa sucesso. ``ml_conexao`` e
``ml_relatorio_conexao`` continuam responsáveis pela autenticação e persistência de
suas respectivas sessões.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import queue
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
MOBILE_MAX_WIDTH = 767
MOBILE_WIDTH = (360, 430)
MOBILE_HEIGHT = (640, 932)
DESKTOP_WIDTH = (1024, 1440)
DESKTOP_HEIGHT = (700, 1000)

# Qualidade 78 num viewport de 1280x800 dava ~150 KB por quadro: a 14 FPS são mais de
# 2 MB/s por aba, mais do que a maioria das conexões entrega. O excesso não vira imagem
# melhor, vira fila — o quadro chegava atrasado, o watchdog acusava queda e o aviso de
# reconexão cobria o login.
#
# Duas qualidades, não uma. Quem lê um CAPTCHA ou confere o que digitou está com a tela
# PARADA: é ali que a nitidez importa, e ali a captura acontece 1x/s — cabe pagar 70.
# Enquanto o usuário mexe, cada quadro vive ~100ms e é substituído antes de alguém
# conseguir olhar; 45 corta ~40% dos bytes e do tempo de encode do Chromium, que é
# exatamente o que estava entre o clique e o pixel. A troca de qualidade muda o digest,
# então o primeiro quadro depois que a tela para já sai nítido pelo caminho normal.
CAPTURE_QUALITY = 70
CAPTURE_BURST_QUALITY = 45
# Três faixas de cadência, escolhidas pelo tempo desde o último input do usuário.
# 250ms fixos (4 FPS) davam um eco de quase meio segundo por tecla e tornavam captcha
# de arrastar impossível de resolver. A rajada de ~10 FPS existe SÓ enquanto o usuário
# está mexendo: o pico de CPU dura poucos segundos e no resto do tempo a captura fica
# mais barata do que era antes, o que importa numa máquina que divide 2 vCPU com o
# gunicorn e os workers de automação.
CAPTURE_BURST_INTERVAL_S = 0.1
CAPTURE_ACTIVE_INTERVAL_S = 0.25
CAPTURE_IDLE_INTERVAL_S = 1.0
BURST_WINDOW_S = 1.5
ACTIVE_WINDOW_S = 5.0
# Piso para a captura que segue um input RECÉM-APLICADO. Os 100ms da rajada existem
# para conter CPU no rastro de uma interação; logo depois de aplicar o clique eles
# viravam espera pura — até 100ms entre apertar e ver, no único instante em que o
# usuário está olhando para aquilo. O custo do próprio screenshot já é o limitador
# real; este piso só evita uma enxurrada de capturas durante um arraste.
CAPTURE_INPUT_INTERVAL_S = 0.03
# Passo do laço dos workers de login — ver passo_do_loop_ms.
LOOP_BURST_MS = 10
LOOP_IDLE_MS = 120
# Silêncio mínimo antes de o worker voltar a gastar CDP com inspeção — ver
# pode_inspecionar.
INSPECAO_APOS_INPUT_S = 0.6
# O gerador SSE não faz mais polling: ele dorme em ``LiveRuntime.frame_event`` e é
# acordado pela própria captura. As constantes de intervalo saíram junto.
# 4s, não 10s: o heartbeat é a única prova de vida quando a tela não muda (CAPTCHA
# sendo lido, formulário parado). Com 10s ele mal cabia no watchdog de 12s do cliente,
# que então acusava queda numa sessão perfeitamente saudável.
HEARTBEAT_INTERVAL_S = 4.0
# Idade máxima do último frame antes do stream ser dado como interrompido. É o
# mesmo limite do watchdog do cliente (ml_conexao.html, 12s): os dois lados
# precisam concordar sobre o estado, senão o badge alterna sem parar entre
# 'reconectando' (servidor) e 'interrompida' (cliente).
STREAM_STALE_MS = 12000
MAX_EVENTOS_POR_POST = 60
MAX_QUEUE_SIZE = 2000
# Quantos caracteres o front pode agrupar num evento `char`. Era 16, escolhido quando
# a fusão só acontecia entre dois POSTs; agora o cliente também funde o que é digitado
# DURANTE um POST em voo, e 16 partia uma frase de senha em blocos que viravam
# chamadas CDP separadas. 64 cabe numa senha inteira e continua sendo um teto barato.
MAX_TEXTO_POR_EVENTO = 64
# Teto da duração do toque repassada ao Chromium — ver despachar_input.
MAX_HOLD_MS = 120

# Os três live views (site ML, relatórios ML e Associados Amazon) vivem no mesmo
# processo gunicorn. Eles não podem usar ResourceLease: a role web é tenant-only e
# a tabela do control plane tem FORCE RLS. Um semáforo compartilhado no processo
# ainda é suficiente para o recurso que realmente dividem — CPU/memória da VM web.
# Scrapers e report sync, em outros process groups, continuam coordenados pelos
# leases PostgreSQL duráveis.
_INTERACTIVE_BROWSER_CAPACITY = max(
    1, int(getattr(settings, "CHROMIUM_GLOBAL_SLOTS", 1)),
)
_interactive_browser_slots = threading.BoundedSemaphore(
    _INTERACTIVE_BROWSER_CAPACITY,
)


class InteractiveBrowserCapacityError(RuntimeError):
    """A VM web já está atendendo outro login interativo."""


@contextmanager
def interactive_browser_slot():
    acquired = _interactive_browser_slots.acquire(blocking=False)
    if not acquired:
        raise InteractiveBrowserCapacityError(
            "Outro login interativo já está usando o navegador. "
            "Aguarde ele terminar ou cancele-o antes de tentar novamente."
        )
    try:
        yield
    finally:
        _interactive_browser_slots.release()

SPECIAL_KEYS = frozenset({
    "Enter", "Backspace", "Tab", "Delete", "Escape",
    "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End",
})


def _int_limited(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def normalizar_viewport(client: dict | None) -> dict:
    """Normaliza o viewport enviado pelo browser sem confiar no payload.

    O corte mobile considera a largura bruta informada pelo cliente. Valores ausentes
    preservam o viewport desktop legado para manter chamadas antigas compatíveis.
    """
    client = client if isinstance(client, dict) else {}
    viewport = client.get("viewport")
    viewport = viewport if isinstance(viewport, dict) else {}
    raw_width = viewport.get("width", DEFAULT_VIEWPORT["width"])
    try:
        mobile = int(raw_width) <= MOBILE_MAX_WIDTH
    except (TypeError, ValueError):
        mobile = False

    if mobile:
        width = _int_limited(raw_width, 390, *MOBILE_WIDTH)
        height = _int_limited(viewport.get("height"), 844, *MOBILE_HEIGHT)
        device_class = "mobile"
    else:
        width = _int_limited(raw_width, DEFAULT_VIEWPORT["width"], *DESKTOP_WIDTH)
        height = _int_limited(
            viewport.get("height"), DEFAULT_VIEWPORT["height"], *DESKTOP_HEIGHT,
        )
        device_class = "desktop"

    try:
        dpr = float(client.get("device_pixel_ratio", 1))
    except (TypeError, ValueError):
        dpr = 1.0
    dpr = max(1.0, min(2.0, dpr))
    pointer = client.get("pointer")
    if pointer not in {"coarse", "fine"}:
        pointer = "coarse" if mobile else "fine"
    return {
        "width": width,
        "height": height,
        "device_pixel_ratio": round(dpr, 2),
        "pointer": pointer,
        "device_class": device_class,
    }


def seq_de(evento) -> int | None:
    """Número de sequência de um evento, mesmo quando o resto é inválido.

    O ACK precisa avançar sobre eventos rejeitados: sem isso um único evento
    descartado abre uma lacuna que nunca fecha, o ack congela e o cliente
    reenvia o mesmo buffer para sempre (a digitação trava).
    """
    if not isinstance(evento, dict):
        return None
    try:
        seq = int(evento.get("seq"))
    except (TypeError, ValueError):
        return None
    return seq if seq >= 1 else None


def limpar_evento(evento: dict, width: int, height: int) -> dict | None:
    """Valida um evento do cliente e devolve somente os campos aceitos."""
    if not isinstance(evento, dict):
        return None
    kind = evento.get("t")
    clean = {"t": kind}
    try:
        clean["seq"] = int(evento.get("seq"))
    except (TypeError, ValueError):
        return None
    if clean["seq"] < 1:
        return None

    if kind in {"move", "down", "up", "click", "wheel"}:
        try:
            clean["x"] = max(0, min(width, int(evento.get("x", 0))))
            clean["y"] = max(0, min(height, int(evento.get("y", 0))))
        except (TypeError, ValueError):
            return None
        if kind in {"down", "up", "click"}:
            clean["button"] = (
                evento.get("button")
                if evento.get("button") in {"left", "right", "middle"}
                else "left"
            )
            clean["clickCount"] = _int_limited(evento.get("clickCount"), 1, 1, 3)
            if kind == "click":
                # Duração real do toque medida no cliente — ver despachar_input.
                clean["holdMs"] = _int_limited(
                    evento.get("holdMs"), 0, 0, MAX_HOLD_MS,
                )
        elif kind == "wheel":
            try:
                clean["dx"] = float(evento.get("dx", 0))
                clean["dy"] = float(evento.get("dy", 0))
            except (TypeError, ValueError):
                return None
        else:
            clean["buttons"] = _int_limited(evento.get("buttons"), 0, 0, 7)
        return clean

    if kind == "char":
        text = str(evento.get("text", ""))[:MAX_TEXTO_POR_EVENTO]
        if not text:
            return None
        clean["text"] = text
        return clean

    if kind == "key" and evento.get("key") in SPECIAL_KEYS:
        clean["key"] = evento["key"]
        return clean
    return None


@dataclass
class LiveRuntime:
    user_id: int
    viewport: dict
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    created_at: float = field(default_factory=time.time)
    input_queue: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=MAX_QUEUE_SIZE),
    )
    frame_data: str = ""
    frame_hash: str = ""
    frame_seq: int = 0
    frame_at: float = 0.0
    first_frame_at: float = 0.0
    last_capture_at: float = 0.0
    # Relógio monotônico do último input aceito. É ele que decide a cadência da
    # captura e do stream — ver CAPTURE_BURST_INTERVAL_S.
    last_input_at: float = 0.0
    input_ack: int = 0
    input_retries: int = 0
    stream_state: str = "reconectando"
    closed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Acordado por ``capture`` quando há quadro NOVO. O gerador SSE dormia num
    # ``time.sleep`` fixo e só descobria o quadro na volta seguinte: até 50ms de
    # atraso puro no fim do caminho, mais 20-50 acordadas/s de uma thread que na
    # maior parte do tempo não tinha nada a fazer.
    frame_event: threading.Event = field(default_factory=threading.Event)
    # Acorda a thread dona do Playwright assim que um POST de input ou comando da UI
    # entra. Sem esse sinal o primeiro clique/tecla depois de uma pausa aguardava o
    # repouso inteiro do worker (até LOOP_IDLE_MS) antes de chegar ao Chromium.
    input_event: threading.Event = field(default_factory=threading.Event)
    # Continua verdadeiro até existir uma captura DEPOIS do input aplicado. Um input
    # que chegava logo após outra captura falhava no piso de 30ms e, na volta seguinte,
    # caía no intervalo normal de 100ms; esse caso criava o pior eco justamente nos
    # cliques e teclas mais rápidos.
    input_feedback_pending: bool = False

    def public_state(self) -> dict:
        with self.lock:
            frame_age_ms = (
                max(0, round((time.time() - self.frame_at) * 1000))
                if self.frame_at else None
            )
            # Quem decide se a imagem parou é a IDADE do último frame, não a última
            # falha de screenshot. Sem esta derivação, uma sequência de capturas
            # falhas (anti-bot do ML segurando a navegação) deixava o servidor em
            # 'reconectando' enquanto o watchdog do cliente dizia 'interrompida' —
            # e o poll aplicava um estado diferente a cada 3s, oscilando o badge.
            estado = self.stream_state
            if frame_age_ms is not None and frame_age_ms > STREAM_STALE_MS:
                estado = "interrompida"
            return {
                "session_id": self.session_id,
                "viewport": dict(self.viewport),
                "stream": {
                    "estado": estado,
                    "frame_seq": self.frame_seq,
                    "frame_at": self.frame_at,
                    "frame_age_ms": frame_age_ms,
                    "first_frame_ms": (
                        max(0, round((self.first_frame_at - self.created_at) * 1000))
                        if self.first_frame_at else None
                    ),
                    "input_ack": self.input_ack,
                    "input_retries": self.input_retries,
                },
            }


def em_rajada(runtime: LiveRuntime, *, now: float) -> bool:
    """True enquanto o usuário está interagindo agora (ou acabou de interagir)."""
    return bool(runtime.last_input_at) and now - runtime.last_input_at <= BURST_WINDOW_S


def passo_do_loop_ms(runtime: LiveRuntime, *, now: float | None = None) -> int:
    """Quanto o worker do browser dorme entre uma volta e a próxima.

    Era fixo em 50ms: um evento recém-chegado esperava 25ms em média sem ninguém
    drenar a fila, e o worker acordava 20x/s pelos 10 minutos inteiros do deadline
    mesmo com o usuário lendo um SMS. Os dois lados estavam errados na mesma
    constante. Agora o passo segue a janela de rajada — mais rápido enquanto o
    usuário mexe, e bem mais barato quando ele não mexe.

    Este valor é o teto da espera interrompível em ``aguardar_atividade``. Depois da
    espera, uma chamada mínima ao Playwright bombeia eventos de popup/nova aba.
    """
    now = time.monotonic() if now is None else now
    return LOOP_BURST_MS if em_rajada(runtime, now=now) else LOOP_IDLE_MS


def aguardar_atividade(page, runtime: LiveRuntime) -> bool:
    """Espera sem impor latência ao próximo input e bombeia eventos do Playwright.

    ``page.wait_for_timeout(LOOP_IDLE_MS)`` não pode ser interrompido pela thread do
    request que recebe mouse/teclado. O Event preserva o repouso barato quando nada
    acontece, mas devolve o controle no instante em que a fila ganha trabalho. A
    chamada curta ao Playwright mantém callbacks de popup/nova aba sendo entregues.
    """
    recebeu_input = runtime.input_event.wait(passo_do_loop_ms(runtime) / 1000)
    if recebeu_input:
        runtime.input_event.clear()
    page.wait_for_timeout(1)
    return recebeu_input


def pode_inspecionar(runtime: LiveRuntime, *, now: float | None = None) -> bool:
    """False enquanto o usuário está clicando/digitando NESTE instante.

    As inspeções periódicas do worker (``storage_state``, detecção de desafio de
    QR, detecção da página de erro, controles do Link Builder) custam de um a três
    round-trips CDP cada, na MESMA thread que precisa despachar o clique e tirar o
    screenshot. Quando o tick delas caía no meio de uma digitação, o eco daquela
    tecla dobrava. Nada que elas observam — cookie de login, texto da página —
    muda em meio segundo, então adiar a volta é de graça.
    """
    now = time.monotonic() if now is None else now
    if not runtime.last_input_at:
        return True
    return now - runtime.last_input_at >= INSPECAO_APOS_INPUT_S


def intervalo_de_captura(runtime: LiveRuntime, *, now: float, active: bool) -> float:
    """Cadência da próxima captura, pelo tempo desde o último input do usuário.

    ``active`` significa que o worker acabou de aplicar input nesta volta do loop:
    o quadro que interessa é o DESTE input, então a captura sai no piso mínimo. A
    janela em ``last_input_at`` mantém a rajada por um instante depois disso, porque
    o efeito de um clique (menu abrindo, campo validando, peça de captcha voltando
    ao lugar) costuma continuar rendendo quadros por mais de um tick — era aí que a
    tela parecia travar em degraus.
    """
    if active or runtime.input_feedback_pending:
        return CAPTURE_INPUT_INTERVAL_S
    desde_input = now - runtime.last_input_at
    if runtime.last_input_at and desde_input <= BURST_WINDOW_S:
        return CAPTURE_BURST_INTERVAL_S
    if runtime.last_input_at and desde_input <= ACTIVE_WINDOW_S:
        return CAPTURE_ACTIVE_INTERVAL_S
    return CAPTURE_IDLE_INTERVAL_S


def qualidade_de_captura(runtime: LiveRuntime, *, now: float, active: bool) -> int:
    """Qualidade JPEG do próximo quadro — ver CAPTURE_QUALITY."""
    if active or em_rajada(runtime, now=now):
        return CAPTURE_BURST_QUALITY
    return CAPTURE_QUALITY


class LiveTransport:
    """Registro isolado de sessões para um fluxo de login."""

    def __init__(self, name: str):
        self.name = name
        self._runtimes: dict[int, LiveRuntime] = {}
        self._lock = threading.Lock()

    def create(self, user_id: int, client: dict | None = None) -> LiveRuntime:
        runtime = LiveRuntime(user_id=user_id, viewport=normalizar_viewport(client))
        with self._lock:
            previous = self._runtimes.get(user_id)
            if previous:
                previous.closed = True
                # Acorda o gerador SSE da sessão antiga: ele dorme no frame_event e
                # sem isto seguraria a thread do gunicorn até o heartbeat vencer.
                previous.frame_event.set()
                previous.input_event.set()
            self._runtimes[user_id] = runtime
        return runtime

    def get(self, user_id: int) -> LiveRuntime | None:
        return self._runtimes.get(user_id)

    def finish(self, user_id: int, runtime: LiveRuntime | None = None) -> None:
        with self._lock:
            current = self._runtimes.get(user_id)
            if runtime is not None and current is not runtime:
                return
            if current:
                current.closed = True
                current.stream_state = "interrompida"
                current.frame_event.set()
                current.input_event.set()
            self._runtimes.pop(user_id, None)

    def status(self, user_id: int) -> dict:
        runtime = self.get(user_id)
        return runtime.public_state() if runtime else {}

    def wake(self, user_id: int) -> None:
        """Desperta o worker para comandos de UI que não passam pela fila de input."""
        runtime = self.get(user_id)
        if runtime is not None and not runtime.closed:
            runtime.input_event.set()

    def enqueue(self, user_id: int, session_id: str, eventos) -> dict:
        runtime = self.get(user_id)
        if runtime is None or runtime.closed:
            return {"ok": False, "erro": "sessao_inativa", "ack": 0}
        if not session_id or not secrets.compare_digest(
            str(session_id), runtime.session_id,
        ):
            return {
                "ok": False,
                "erro": "sessao_desatualizada",
                "ack": runtime.input_ack,
            }
        if not isinstance(eventos, list):
            return {"ok": False, "erro": "payload_invalido", "ack": runtime.input_ack}

        validos, rejeitados = {}, set()
        for event in eventos[:MAX_EVENTOS_POR_POST]:
            clean = limpar_evento(
                event, runtime.viewport["width"], runtime.viewport["height"],
            )
            if clean is not None:
                validos[clean["seq"]] = clean
                continue
            # Evento descartado de propósito (coordenada inválida, tecla fora da
            # lista). O seq ainda precisa ser consumido, senão a faixa do ACK
            # nunca mais avança.
            seq = seq_de(event)
            if seq is not None:
                rejeitados.add(seq)

        accepted = 0
        with runtime.lock:
            if any(seq <= runtime.input_ack for seq in validos):
                runtime.input_retries += 1
            # O ACK continua representando uma faixa contínua: só uma lacuna REAL
            # (evento que ainda não chegou) interrompe o avanço, e aí o cliente
            # reenvia o buffer.
            proximo = runtime.input_ack + 1
            while True:
                if proximo in rejeitados:
                    runtime.input_ack = proximo
                    proximo += 1
                    continue
                clean = validos.get(proximo)
                if clean is None:
                    break
                try:
                    runtime.input_queue.put_nowait(clean)
                except queue.Full:
                    # Fila cheia: não confirmar, para o cliente reenviar.
                    break
                runtime.input_ack = proximo
                accepted += 1
                proximo += 1
            ack = runtime.input_ack
            if accepted:
                # Abre a janela de rajada: o usuário está interagindo agora e é neste
                # instante que ele precisa ver o resultado do clique/tecla.
                runtime.last_input_at = time.monotonic()
                # Interrompe imediatamente o repouso da thread dona do Chromium.
                runtime.input_event.set()
        return {"ok": True, "aceitos": accepted, "ack": ack}

    def capture(self, runtime: LiveRuntime, page, *, active: bool = False) -> bool:
        """Publica uma captura quando o intervalo de atividade permite."""
        if active:
            # Marcar ANTES de testar o intervalo é essencial: caso uma captura tenha
            # acabado há menos de 30ms, a pendência sobrevive para a próxima volta.
            runtime.input_feedback_pending = True
        now = time.monotonic()
        interval = intervalo_de_captura(runtime, now=now, active=active)
        if now - runtime.last_capture_at < interval:
            return False
        try:
            raw = page.screenshot(
                type="jpeg",
                quality=qualidade_de_captura(runtime, now=now, active=active),
                scale="css",
            )
            if not isinstance(raw, (bytes, bytearray)):
                return False
        except Exception:
            # Depois do primeiro frame, uma falha pontual de screenshot (anti-bot
            # do ML segurando a navegação, troca de aba) NÃO rebaixa o estado:
            # public_state marca 'interrompida' pela idade do frame. Sem esta
            # guarda o estado oscilava ao_vivo/reconectando a cada falha.
            if not runtime.frame_seq:
                runtime.stream_state = "reconectando"
            logger.warning(
                "Captura do login %s falhou (user=%s).",
                self.name, runtime.user_id, exc_info=True,
            )
            return False
        wall_now = time.time()
        digest = hashlib.blake2b(raw, digest_size=16).hexdigest()
        # Tela idêntica à anterior não vira quadro novo. Reenviar os mesmos ~80 KB a
        # cada volta (um formulário parado é o estado normal enquanto se lê o e-mail
        # ou se espera o SMS) só ocupava a banda de que o próximo quadro REAL ia
        # precisar. O `frame_at` continua avançando: a sessão está viva, não parada.
        if digest == runtime.frame_hash and runtime.frame_seq:
            with runtime.lock:
                runtime.frame_at = wall_now
                runtime.last_capture_at = now
                runtime.stream_state = "ao_vivo"
                runtime.input_feedback_pending = False
            return False
        data = base64.b64encode(raw).decode("ascii")
        first_frame = False
        with runtime.lock:
            runtime.frame_seq += 1
            runtime.frame_hash = digest
            runtime.frame_data = data
            runtime.frame_at = wall_now
            runtime.last_capture_at = now
            runtime.input_feedback_pending = False
            if not runtime.first_frame_at:
                runtime.first_frame_at = wall_now
                first_frame = True
            runtime.stream_state = "ao_vivo"
        # Fora do lock: o gerador SSE acorda e vai querer o mesmo lock para ler o seq.
        runtime.frame_event.set()
        if first_frame:
            logger.info(
                "ml_login_metric transport=%s user=%s first_frame_ms=%s",
                self.name, runtime.user_id,
                max(0, round((wall_now - runtime.created_at) * 1000)),
            )
        return True

    def frames(self, user_id: int, session_id: str | None = None):
        """Gera eventos estruturados para a view SSE.

        A sessão pode levar alguns instantes para ser registrada depois do POST start,
        por isso existe uma tolerância inicial curta.
        """
        waiting = 0
        runtime = None
        while runtime is None and waiting < 60:
            runtime = self.get(user_id)
            if runtime is None:
                waiting += 1
                time.sleep(0.05)
        if runtime is None:
            return
        if session_id and not secrets.compare_digest(str(session_id), runtime.session_id):
            yield {"event": "error", "data": "sessao_desatualizada"}
            return

        last_seq = 0
        last_emit = time.monotonic()
        while not runtime.closed:
            # Limpar ANTES de ler: se a captura sinalizar entre o clear e a leitura, o
            # seq novo já aparece aqui; se sinalizar depois da leitura, o evento fica
            # armado e o `wait` abaixo volta na hora. Nenhuma das ordens perde quadro.
            runtime.frame_event.clear()
            data = ""
            with runtime.lock:
                seq = runtime.frame_seq
                # Copiar a string base64 (dezenas de KB) a cada volta do laço mesmo sem
                # frame novo era trabalho puro dentro do lock que o worker do browser
                # também disputa. Agora só o inteiro do seq é lido no caso comum.
                if seq > last_seq:
                    data = runtime.frame_data
            if data:
                last_seq = seq
                last_emit = time.monotonic()
                yield {"event": "frame", "id": seq, "data": data}
                continue
            restante = HEARTBEAT_INTERVAL_S - (time.monotonic() - last_emit)
            if restante <= 0:
                last_emit = time.monotonic()
                yield {"event": "heartbeat", "data": str(last_seq)}
                continue
            # Dorme até a captura publicar um quadro ou o heartbeat vencer, em vez de
            # acordar 20-50x/s para descobrir que nada mudou. O quadro sai do servidor
            # no instante em que existe.
            #
            # O teto de 0,25s é a rede de segurança: `frame_event` é UM evento por
            # sessão, e durante a janela em que um stream antigo ainda não percebeu
            # que o cliente sumiu os dois generators competem pelo mesmo sinal — quem
            # perder o wakeup esperaria o heartbeat inteiro, e 4s de imagem parada é
            # exatamente o congelamento que este trabalho existe para eliminar. Com o
            # teto o custo cai para 250ms, e no caso normal o wait continua voltando
            # no instante da captura.
            runtime.frame_event.wait(timeout=min(restante, 0.25))


class ActivePage:
    """Mantém a aba realmente ativa quando desafios abrem popup/nova página."""

    def __init__(self, context, initial_page, runtime: LiveRuntime):
        self._pages = [initial_page]
        self._active = initial_page
        self._runtime = runtime
        context.on("page", self._on_page)

    def _on_page(self, page):
        self._pages.append(page)
        self._active = page
        # Popup antes do primeiro frame = ainda conectando; depois dele, a troca
        # de aba não derruba o estado — a próxima captura já vem da aba nova.
        if not self._runtime.frame_seq:
            self._runtime.stream_state = "reconectando"

    def current(self):
        alive = []
        for page in self._pages:
            try:
                # ``is_closed`` devolve bool no Playwright; comparar com ``True``
                # mantém doubles de teste simples compatíveis.
                if page.is_closed() is not True:
                    alive.append(page)
            except Exception:
                continue
        self._pages = alive
        if self._active not in alive and alive:
            self._active = alive[-1]
            if not self._runtime.frame_seq:
                self._runtime.stream_state = "reconectando"
        return self._active if alive else None


def despachar_input(page, event: dict) -> None:
    """Aplica um evento validado sem registrar seu conteúdo."""
    kind = event.get("t")
    try:
        if kind == "move":
            page.mouse.move(event["x"], event["y"])
        elif kind == "down":
            page.mouse.move(event["x"], event["y"])
            page.mouse.down(
                button=event.get("button", "left"),
                click_count=event.get("clickCount", 1),
            )
        elif kind == "up":
            page.mouse.move(event["x"], event["y"])
            page.mouse.up(
                button=event.get("button", "left"),
                click_count=event.get("clickCount", 1),
            )
        elif kind == "click":
            # Clique simples chega como uma operação atômica. Enviar ``down`` e
            # ``up`` em dois POSTs serializados deixava o botão pressionado por um
            # round-trip inteiro; em desafios de selecionar imagens o site às vezes
            # descartava esse gesto artificialmente longo. Arrastes continuam usando
            # down/move/up e, portanto, não perdem a semântica de pressão contínua.
            #
            # ``holdMs`` é a duração que o usuário REALMENTE segurou o botão, medida
            # no cliente e limitada a MAX_HOLD_MS. Sem ela o Playwright solta o botão
            # no mesmo instante em que o aperta, e um toque de 0ms é um gesto que
            # componente nenhum vê num usuário de verdade — parte dos widgets de
            # desafio simplesmente ignora. Reproduzir o toque medido é mais fiel do
            # que arbitrar uma constante, e o teto impede que uma sessão presa num
            # botão segure a thread do worker.
            page.mouse.click(
                event["x"], event["y"],
                button=event.get("button", "left"),
                click_count=event.get("clickCount", 1),
                delay=event.get("holdMs", 0),
            )
        elif kind == "wheel":
            page.mouse.move(event["x"], event["y"])
            page.mouse.wheel(event.get("dx", 0), event.get("dy", 0))
        elif kind == "char" and event.get("text"):
            # ``type`` emite um par de eventos CDP para CADA caractere. O front já
            # agrupa texto em blocos de até 16 caracteres; ``insert_text`` aplica o
            # bloco inteiro em uma única operação e mantém a ordem com Enter,
            # Backspace etc., que continuam passando pelo ramo ``key`` abaixo.
            page.keyboard.insert_text(str(event["text"]))
        elif kind == "key" and event.get("key") in SPECIAL_KEYS:
            page.keyboard.press(event["key"])
    except Exception:
        logger.debug("Evento de input remoto descartado (%s).", kind, exc_info=True)
