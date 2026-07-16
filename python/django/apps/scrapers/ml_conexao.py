"""Conexão web do Mercado Livre — login sem script local, sem colar auth.json.

Substitui a gambiarra de "rode connect_ml.py no seu PC e cole o auth.json". Num
servidor headless não dá pra abrir um browser pro usuário clicar, então rodamos o
Chromium NA PRÓPRIA MÁQUINA (o mesmo que o scraper já usa — Playwright/Chromium já
está na imagem) e transmitimos a tela pro navegador do usuário via *live view*: um
screencast do CDP (`Page.startScreencast`) desenhado num <canvas>, com o mouse e o
teclado dele encaminhados de volta (`Input.dispatch*`). Ele loga no ML ali dentro —
no celular ou no desktop — e quando a sessão fica válida capturamos o storage_state
e salvamos no mesmo `auth_{id}.json` que o resto do scraper já espera.

Isso troca o antigo Browserbase (browser hospedado pago; o free plan estourava com
402 Payment Required). Custo zero, sem colar nada, e a senha é digitada direto na
página REAL do ML — não passa pelo nosso backend.

Fluxo (espelha o QR do WhatsApp):
  1. criar_sessao(user)  -> sobe o Chromium local, navega pro login do ML, começa o
     screencast numa thread que fica observando o login.
  2. front abre um EventSource em frames() e desenha cada frame no <canvas>; captura
     mouse/teclado e faz POST em enfileirar_input().
  3. thread detecta o redirect pós-login -> salva auth_{id}.json -> fase 'conectado'.

Estado compartilhado (fase/erro) vai pro cache (Redis/DB em prod) pra funcionar entre
threads do gunicorn; a thread que segura o browser vive em um worker só, e os frames
e a fila de input ficam em dicts em memória desse mesmo processo (1 worker no Fly).
"""
import logging
import os
import queue
import threading
import time

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/"
HOME_HOST = "mercadolivre.com.br"

LOGIN_DEADLINE_S = 600           # tempo máx. esperando o usuário logar
LOOP_MS = 50                     # granularidade do worker (bombeia CDP + drena input)

# Viewport remoto. O front escala o <canvas> pra caber na tela mantendo a proporção.
VIEW_W, VIEW_H = 1280, 800
SCREENCAST = {"format": "jpeg", "quality": 55,
              "maxWidth": VIEW_W, "maxHeight": VIEW_H, "everyNthFrame": 1}

MAX_EVENTOS_POR_POST = 60        # teto de eventos por request (anti-abuso da fila)

# Estado em memória DESTE worker (o cache guarda fase/erro visível entre threads).
_threads: dict[int, threading.Thread] = {}
_frames: dict[int, str] = {}                     # último frame base64 por usuário
_inputs: dict[int, "queue.Queue"] = {}           # eventos de input pendentes por usuário
_lock = threading.Lock()

# Teclas não-imprimíveis que o front manda como {t:'key', key:'Enter'}; imprimíveis vêm
# como {t:'char', text:'a'}. Os dois casos vão pro page.keyboard do Playwright, que já
# tem o mapa tecla->code/keyCode (USKeyboardLayout) e emite keydown/keypress/keyup de
# verdade. Estes nomes são os mesmos que keyboard.press() aceita.
_SPECIAL_KEYS = frozenset({
    "Enter", "Backspace", "Tab", "Delete", "Escape",
    "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End",
})


def _cache_key(user_id: int) -> str:
    return f"ml_conexao:{user_id}"


def _set_estado(user_id: int, **campos):
    estado = cache.get(_cache_key(user_id)) or {}
    estado.update(campos)
    estado["atualizado_em"] = time.time()
    # TTL um pouco acima do deadline pra não sumir no meio do login.
    cache.set(_cache_key(user_id), estado, timeout=LOGIN_DEADLINE_S + 120)
    return estado


def _auth_path(user_id: int) -> str:
    """Onde salvar a sessão do ML deste usuário (o que link.py/auxiliar.py leem)."""
    return os.path.join(settings.ML_AUTH_DIR, f"auth_{user_id}.json")


def status(user_id: int) -> dict:
    """Estado atual da conexão pro polling do front.

    fase: 'idle' | 'iniciando' | 'aguardando_login' | 'salvando' | 'conectado' | 'erro'
    """
    estado = cache.get(_cache_key(user_id)) or {"fase": "idle"}
    # 'conectado' de verdade = arquivo existe e está fresco (mesma regra do monitor).
    try:
        from apps.scrapers.monitor_conexao import ml_conectado
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(id=user_id).first()
        estado["auth_valido"] = ml_conectado(user) if user else False
    except Exception:
        estado["auth_valido"] = os.path.exists(_auth_path(user_id))
    return estado


def _url_logada(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if "login" in u or "/jms/" in u or "hub.mercadolibre" in u:
        return False
    return HOME_HOST in u or "mercadolibre.com" in u


def _despachar_input(cdp, page, ev: dict):
    """Traduz UM evento do front em input no Chromium local. Nunca levanta.

    Mouse vai por CDP cru (dispatchMouseEvent aceita coordenada; page.mouse também,
    mas o CDP evita a ida-e-volta de estado do Playwright). Teclado vai por
    page.keyboard: Input.insertText insere texto SEM disparar keydown/keypress/keyup,
    e a página de login do ML ignora o que digita assim — o mouse funcionava e o texto
    não entrava. page.keyboard.type() reusa o mapa de teclas do Playwright e emite os
    eventos completos, que é o que o usuário de fato digitou do outro lado.
    """
    try:
        t = ev.get("t")
        if t == "move":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": ev["x"], "y": ev["y"],
                "button": "none", "buttons": int(ev.get("buttons", 0))})
        elif t == "down":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed", "x": ev["x"], "y": ev["y"],
                "button": ev.get("button", "left"), "buttons": 1,
                "clickCount": int(ev.get("clickCount", 1))})
        elif t == "up":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": ev["x"], "y": ev["y"],
                "button": ev.get("button", "left"), "buttons": 0,
                "clickCount": int(ev.get("clickCount", 1))})
        elif t == "wheel":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": ev["x"], "y": ev["y"],
                "deltaX": float(ev.get("dx", 0)), "deltaY": float(ev.get("dy", 0))})
        elif t == "char":
            texto = str(ev.get("text", ""))[:8]
            if texto:
                # delay=0: a cadência real já é a do usuário; o front manda cada tecla
                # assim que ela acontece.
                page.keyboard.type(texto, delay=0)
        elif t == "key":
            if ev.get("key") in _SPECIAL_KEYS:
                page.keyboard.press(ev["key"])
    except Exception:
        # Um evento malformado/tardio (browser fechando) não pode derrubar o worker.
        # Em debug dá pra ver o que morreu — foi o silêncio aqui que escondeu o
        # teclado quebrado por tanto tempo.
        logger.debug("Evento de input descartado (%s)", ev.get("t"), exc_info=True)


def _worker(user_id: int):
    """Sobe o Chromium local, transmite a tela e espera o usuário concluir o login."""
    from playwright.sync_api import sync_playwright
    from apps.scrapers.auxiliar import ua_aleatorio

    # Fila limitada: um cliente que floode input só enche a PRÓPRIA fila; o excesso é
    # descartado (enfileirar_input trata queue.Full) sem estourar memória do processo.
    fila = queue.Queue(maxsize=2000)
    with _lock:
        _inputs[user_id] = fila
        _frames.pop(user_id, None)

    try:
        _set_estado(user_id, fase="iniciando", erro="")
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                # Mesmos flags do scraper (auxiliar.iniciar_browser) + dev-shm p/ não
                # crashar o Chromium em container com /dev/shm pequeno (Fly).
                args=["--disable-blink-features=AutomationControlled",
                      "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                viewport={"width": VIEW_W, "height": VIEW_H},
                user_agent=ua_aleatorio(),
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            cdp = context.new_cdp_session(page)

            def _on_frame(params):
                # Guarda só o último frame (coalesce): o SSE lê o mais recente.
                _frames[user_id] = params.get("data", "")
                try:
                    cdp.send("Page.screencastFrameAck",
                             {"sessionId": params.get("sessionId")})
                except Exception:
                    pass

            cdp.send("Page.enable")
            # Headless não tem janela, então o Chromium trata o documento como sem foco
            # e JS de login costuma ignorar input nesse estado. Isso força "focado".
            try:
                cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                logger.warning("setFocusEmulationEnabled falhou; seguindo sem ele.")
            cdp.on("Page.screencastFrame", _on_frame)

            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            cdp.send("Page.startScreencast", SCREENCAST)
            _set_estado(user_id, fase="aguardando_login", erro="")

            deadline = time.time() + LOGIN_DEADLINE_S
            logado = False
            last_beat = time.time()
            while time.time() < deadline:
                estado = cache.get(_cache_key(user_id)) or {}
                if estado.get("cancelar"):
                    _set_estado(user_id, fase="idle", erro="")
                    break

                # Drena e aplica os eventos de input acumulados desde a última volta.
                for _ in range(MAX_EVENTOS_POR_POST * 4):
                    try:
                        ev = fila.get_nowait()
                    except queue.Empty:
                        break
                    _despachar_input(cdp, page, ev)

                url_atual = page.url
                # 'salvar_agora' = usuário clicou "já entrei"; força a captura.
                if _url_logada(url_atual) or estado.get("salvar_agora"):
                    logado = True
                    break

                # Heartbeat: renova TTL + atualizado_em sem trocar de fase (o front
                # segue desenhando os frames pelo EventSource).
                if time.time() - last_beat > 8:
                    _set_estado(user_id, fase="aguardando_login")
                    last_beat = time.time()

                # wait_for_timeout bombeia os eventos CDP (o screencastFrame chega aqui).
                page.wait_for_timeout(LOOP_MS)

            if logado:
                _set_estado(user_id, fase="salvando")
                try:
                    cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                context.storage_state(path=_auth_path(user_id))
                _set_estado(user_id, fase="conectado", salvar_agora=False, erro="")
            elif (cache.get(_cache_key(user_id)) or {}).get("fase") != "idle":
                _set_estado(user_id, fase="erro",
                            erro="Tempo esgotado esperando o login. Tente de novo.")
            browser.close()
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira mensagem pro usuário
        _set_estado(user_id, fase="erro", erro=f"Falha na conexão: {exc}")
    finally:
        with _lock:
            _inputs.pop(user_id, None)
            _frames.pop(user_id, None)
            _threads.pop(user_id, None)


def criar_sessao(user) -> dict:
    """Inicia (ou reaproveita) a sessão de login web do ML pro usuário."""
    user_id = user.id
    with _lock:
        viva = _threads.get(user_id)
        if viva and viva.is_alive():
            # Já tem sessão rolando neste worker — devolve o estado atual.
            return status(user_id)
        _set_estado(user_id, fase="iniciando", erro="", cancelar=False, salvar_agora=False)
        t = threading.Thread(target=_worker, args=(user_id,), daemon=True)
        _threads[user_id] = t
        t.start()
    return status(user_id)


def frames(user_id: int):
    """Generator de frames base64 (JPEG) pro SSE. Liveness = a fila do worker existir
    (`_inputs[user_id]`): SSE e worker vivem no MESMO processo (1 gunicorn worker), então
    isso é estado em memória — zero hit no banco no loop de streaming. Encerra quando o
    worker some (login concluído/cancelado/erro) ou após ~30s sem frame novo (o
    EventSource do front reabre sozinho enquanto a fase seguir de conexão)."""
    ultimo = None
    ocioso = 0
    espera_inicio = 0
    while True:
        if user_id not in _inputs:
            # Grace no começo: a thread do worker pode ainda não ter registrado a fila.
            espera_inicio += 1
            if espera_inicio > 60:        # ~3s sem worker -> encerra
                break
            time.sleep(0.05)
            continue
        espera_inicio = 0
        frame = _frames.get(user_id)
        if frame and frame is not ultimo:
            ultimo = frame
            ocioso = 0
            yield frame
        else:
            ocioso += 1
            if ocioso > 600:              # ~30s sem frame novo -> encerra o stream
                break
        time.sleep(0.05)


def enfileirar_input(user_id: int, eventos) -> dict:
    """Recebe eventos de input do front (mouse/teclado) e empurra pra fila do worker.
    Valida tipo/coords/limites — dados do cliente não são confiáveis."""
    fila = _inputs.get(user_id)
    if fila is None:
        return {"ok": False, "erro": "sessao_inativa"}
    if not isinstance(eventos, list):
        return {"ok": False, "erro": "payload_invalido"}
    aceitos = 0
    for ev in eventos[:MAX_EVENTOS_POR_POST]:
        if not isinstance(ev, dict):
            continue
        t = ev.get("t")
        limpo = {"t": t}
        if t in ("move", "down", "up", "wheel"):
            try:
                limpo["x"] = max(0, min(VIEW_W, int(ev.get("x", 0))))
                limpo["y"] = max(0, min(VIEW_H, int(ev.get("y", 0))))
            except (TypeError, ValueError):
                continue
            if t in ("down", "up"):
                limpo["button"] = ev.get("button") if ev.get("button") in (
                    "left", "right", "middle") else "left"
                limpo["clickCount"] = ev.get("clickCount", 1)
            if t == "wheel":
                limpo["dx"] = ev.get("dx", 0)
                limpo["dy"] = ev.get("dy", 0)
            if t == "move":
                limpo["buttons"] = ev.get("buttons", 0)
        elif t == "char":
            limpo["text"] = str(ev.get("text", ""))[:8]
            if not limpo["text"]:
                continue
        elif t == "key":
            if ev.get("key") not in _SPECIAL_KEYS:
                continue
            limpo["key"] = ev.get("key")
        else:
            continue
        try:
            fila.put_nowait(limpo)
            aceitos += 1
        except queue.Full:
            break
    return {"ok": True, "aceitos": aceitos}


def salvar_sessao_manual(user_id: int, raw_json: str) -> dict:
    """Caminho de EMERGÊNCIA (não exposto na UI): valida um storage_state do Playwright
    com cookie do Mercado Livre e grava no mesmo auth_{id}.json que link.py/auxiliar.py
    leem. Mantido como rede de segurança; o fluxo normal é o live view local.

    Retorna o mesmo dict de status() (fase 'conectado' em sucesso, 'erro' senão).
    """
    import json

    texto = (raw_json or "").strip()
    if not texto:
        return _set_estado(user_id, fase="erro",
                           erro="Cole o conteúdo do auth.json (ou envie o arquivo).")
    try:
        dados = json.loads(texto)
    except (ValueError, TypeError):
        return _set_estado(user_id, fase="erro",
                           erro="Isso não é um JSON válido. Cole o conteúdo completo do auth.json.")

    cookies = dados.get("cookies") if isinstance(dados, dict) else None
    if not isinstance(cookies, list) or not cookies:
        return _set_estado(user_id, fase="erro",
                           erro="Arquivo não parece um auth.json do Playwright (sem 'cookies').")
    # Sanidade: precisa de ao menos 1 cookie do domínio do Mercado Livre, senão é
    # sessão de outro site (colou o arquivo errado).
    tem_ml = any(
        ("mercadolivre" in (c.get("domain", "").lower())
         or "mercadolibre" in (c.get("domain", "").lower()))
        for c in cookies if isinstance(c, dict)
    )
    if not tem_ml:
        return _set_estado(user_id, fase="erro",
                           erro="Nenhum cookie do Mercado Livre no arquivo. "
                                "Faça login no ML antes de salvar o auth.json.")

    destino = _auth_path(user_id)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    # Escrita atômica: grava num temporário e renomeia, pra nunca deixar um
    # auth.json truncado se algo falhar no meio.
    tmp = destino + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dados, fh)
        os.replace(tmp, destino)
    except OSError as exc:
        try:
            os.path.exists(tmp) and os.remove(tmp)
        except OSError:
            pass
        return _set_estado(user_id, fase="erro", erro=f"Não foi possível salvar a sessão: {exc}")

    return _set_estado(user_id, fase="conectado", erro="", salvar_agora=False, cancelar=False)


def salvar_agora(user_id: int):
    """Usuário clicou 'já entrei' — pede pra thread capturar a sessão agora."""
    _set_estado(user_id, salvar_agora=True)


def cancelar(user_id: int):
    _set_estado(user_id, cancelar=True)
