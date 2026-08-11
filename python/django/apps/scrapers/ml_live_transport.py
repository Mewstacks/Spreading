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
# reconexão cobria o login. 64 mantém o texto do formulário nítido pela metade do peso.
CAPTURE_QUALITY = 64
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
# O gerador SSE acompanha a rajada; fora dela não faz sentido acordar 20x/s.
STREAM_POLL_BURST_S = 0.02
STREAM_POLL_IDLE_S = 0.05
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

    if kind in {"move", "down", "up", "wheel"}:
        try:
            clean["x"] = max(0, min(width, int(evento.get("x", 0))))
            clean["y"] = max(0, min(height, int(evento.get("y", 0))))
        except (TypeError, ValueError):
            return None
        if kind in {"down", "up"}:
            clean["button"] = (
                evento.get("button")
                if evento.get("button") in {"left", "right", "middle"}
                else "left"
            )
            clean["clickCount"] = _int_limited(evento.get("clickCount"), 1, 1, 3)
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
        text = str(evento.get("text", ""))[:16]
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


def intervalo_de_captura(runtime: LiveRuntime, *, now: float, active: bool) -> float:
    """Cadência da próxima captura, pelo tempo desde o último input do usuário.

    ``active`` significa que o worker acabou de aplicar input nesta volta do loop e
    força a rajada. A janela em ``last_input_at`` mantém a rajada por um instante
    depois disso, porque o efeito de um clique (menu abrindo, campo validando, peça
    de captcha voltando ao lugar) costuma continuar rendendo quadros por mais de um
    tick — era aí que a tela parecia travar em degraus.
    """
    if active:
        return CAPTURE_BURST_INTERVAL_S
    desde_input = now - runtime.last_input_at
    if runtime.last_input_at and desde_input <= BURST_WINDOW_S:
        return CAPTURE_BURST_INTERVAL_S
    if runtime.last_input_at and desde_input <= ACTIVE_WINDOW_S:
        return CAPTURE_ACTIVE_INTERVAL_S
    return CAPTURE_IDLE_INTERVAL_S


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
            self._runtimes.pop(user_id, None)

    def status(self, user_id: int) -> dict:
        runtime = self.get(user_id)
        return runtime.public_state() if runtime else {}

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
        return {"ok": True, "aceitos": accepted, "ack": ack}

    def capture(self, runtime: LiveRuntime, page, *, active: bool = False) -> bool:
        """Publica uma captura quando o intervalo de atividade permite."""
        now = time.monotonic()
        interval = intervalo_de_captura(runtime, now=now, active=active)
        if now - runtime.last_capture_at < interval:
            return False
        try:
            raw = page.screenshot(
                type="jpeg", quality=CAPTURE_QUALITY, scale="css",
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
            return False
        data = base64.b64encode(raw).decode("ascii")
        first_frame = False
        with runtime.lock:
            runtime.frame_seq += 1
            runtime.frame_hash = digest
            runtime.frame_data = data
            runtime.frame_at = wall_now
            runtime.last_capture_at = now
            if not runtime.first_frame_at:
                runtime.first_frame_at = wall_now
                first_frame = True
            runtime.stream_state = "ao_vivo"
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
            data = ""
            with runtime.lock:
                seq = runtime.frame_seq
                # Copiar a string base64 (dezenas de KB) a cada volta do laço mesmo sem
                # frame novo era trabalho puro dentro do lock que o worker do browser
                # também disputa. Agora só o inteiro do seq é lido no caso comum.
                if seq > last_seq:
                    data = runtime.frame_data
                em_rajada = (
                    bool(runtime.last_input_at)
                    and time.monotonic() - runtime.last_input_at <= BURST_WINDOW_S
                )
            if data:
                last_seq = seq
                last_emit = time.monotonic()
                yield {"event": "frame", "id": seq, "data": data}
            elif time.monotonic() - last_emit >= HEARTBEAT_INTERVAL_S:
                last_emit = time.monotonic()
                yield {"event": "heartbeat", "data": str(last_seq)}
            time.sleep(STREAM_POLL_BURST_S if em_rajada else STREAM_POLL_IDLE_S)


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
        elif kind == "wheel":
            page.mouse.move(event["x"], event["y"])
            page.mouse.wheel(event.get("dx", 0), event.get("dy", 0))
        elif kind == "char" and event.get("text"):
            page.keyboard.type(str(event["text"]), delay=0)
        elif kind == "key" and event.get("key") in SPECIAL_KEYS:
            page.keyboard.press(event["key"])
    except Exception:
        logger.debug("Evento de input remoto descartado (%s).", kind, exc_info=True)
