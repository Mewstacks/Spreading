"""Conexão web do Mercado Livre — login sem script local, sem colar auth.json.

Substitui a gambiarra de "rode connect_ml.py no seu PC e cole o auth.json". Num
servidor headless não dá pra abrir um browser pro usuário clicar, então rodamos o
Chromium NA PRÓPRIA MÁQUINA (o mesmo que o scraper já usa — Playwright/Chromium já
está na imagem) e transmitimos capturas nítidas pro navegador do usuário via *live
view*, desenhadas num <canvas>, com mouse e teclado encaminhados de volta. Ele loga
no ML ali dentro — no celular ou no desktop — e quando a sessão fica válida capturamos
o storage_state e o salvamos cifrado por organização.

Isso troca o antigo Browserbase (browser hospedado pago; o free plan estourava com
402 Payment Required). Custo zero e sem colar nada. Cliques e teclas são retransmitidos
ao Chromium somente em memória; o conteúdo não é persistido nem incluído nos logs.

Fluxo (espelha o QR do WhatsApp):
  1. criar_sessao(user)  -> sobe o Chromium local, navega pro login do ML e começa
     a publicar capturas numeradas numa thread que observa o login.
  2. front abre um EventSource em frames() e desenha cada frame no <canvas>; captura
     mouse/teclado e faz POST em enfileirar_input().
  3. thread valida a sessão com uma sonda autenticada -> salva cifrada -> 'conectado'.

Estado compartilhado (fase/erro) vai pro cache (Redis/DB em prod) pra funcionar entre
threads do gunicorn; a thread que segura o browser vive em um worker só, e os frames
e a fila de input ficam em dicts em memória desse mesmo processo (1 worker no Fly).
"""
import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

from django.conf import settings
from django.core.cache import cache

from apps.scrapers.erros_conexao import mensagem_de_erro, novo_codigo
from apps.scrapers.ml_live_transport import (
    ActivePage,
    LiveTransport,
    despachar_input as despachar_input_v2,
)

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/"

LOGIN_DEADLINE_S = 600           # tempo máx. esperando o usuário logar
LOOP_MS = 50                     # granularidade do worker (bombeia CDP + drena input)
GOTO_TIMEOUT_MS = 60000          # em prod (IP de datacenter) o gateway de login demora
GOTO_TENTATIVAS = 2              # timeout na 1ª tenta de novo antes de desistir

# Compatibilidade da conexão Amazon, que ainda importa estes valores e o despachante
# CDP legado. Os dois fluxos do ML usam viewport adaptativo + LiveTransport.
VIEW_W, VIEW_H = 1280, 800

# Parâmetros do screencast legado da Amazon. O ML captura JPEG 78 no viewport efetivo.
SCREENCAST = {"format": "jpeg", "quality": 55,
              "maxWidth": 960, "maxHeight": 600, "everyNthFrame": 2}

MAX_EVENTOS_POR_POST = 60        # teto de eventos por request (anti-abuso da fila)

# Estado em memória DESTE worker (o cache guarda fase/erro visível entre threads).
_threads: dict[int, threading.Thread] = {}
_frames: dict[int, str] = {}                     # último frame base64 por usuário
_inputs: dict[int, "queue.Queue"] = {}           # eventos de input pendentes por usuário
_lock = threading.Lock()
_transport = LiveTransport("mercado_livre")

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
    """Caminho legado exato, mantido somente para o migrador da Fase 0."""
    return os.path.join(settings.ML_AUTH_DIR, f"auth_{user_id}.json")


def status(user_id: int) -> dict:
    """Estado atual da conexão pro polling do front.

    fase: 'idle' | 'iniciando' | 'aguardando_login' | 'salvando' | 'conectado' | 'erro'
    """
    estado = cache.get(_cache_key(user_id)) or {"fase": "idle"}
    # 'conectado' de verdade vem da fonte única (conexoes.py) — a mesma que o
    # dashboard e a Saúde leem. A `fase` acima é só o progresso do login em curso.
    try:
        from django.contrib.auth import get_user_model
        from apps.scrapers.conexoes import estado_ml

        user = get_user_model().objects.filter(id=user_id).first()
        est = estado_ml(user) if user else None
        estado["auth_valido"] = bool(est and est.conectado)
        estado["motivo_desconexao"] = est.motivo if est and not est.conectado else ""
    except Exception:
        from apps.accounts.ml_sessions import has_storage_state
        user = get_user_model().objects.filter(id=user_id).first()
        estado["auth_valido"] = bool(user and has_storage_state(user))
        estado["motivo_desconexao"] = ""
    estado.update(_transport.status(user_id))
    return estado


def _storage_fingerprint(storage_state: dict) -> str:
    """Assinatura somente em memória para detectar mudança de cookies.

    O valor nunca é registrado nem devolvido ao frontend. Ele evita consultar a sonda
    autenticada continuamente enquanto o usuário ainda está parado na mesma etapa.
    """
    cookies = storage_state.get("cookies", []) if isinstance(storage_state, dict) else []
    material = "\n".join(sorted(
        f"{cookie.get('domain', '')}|{cookie.get('name', '')}|{cookie.get('value', '')}"
        for cookie in cookies if isinstance(cookie, dict)
    ))
    return sha256(material.encode("utf-8")).hexdigest()


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


def _ir_para_login(page):
    """Navega até a tela de login do ML, tolerante à lentidão do servidor em prod.

    Local (IP residencial) a página carrega rápido; em prod o IP de datacenter da Fly
    bate no gateway anti-bot da ML, que costuma travar a navegação. Por isso:
    - `wait_until="commit"`: conclui assim que a resposta chega, sem esperar o DOM
      inteiro — o screencast já começa e o usuário vê a tela carregando ao vivo;
    - `timeout` de 60s (não os 30s default) e uma 2ª tentativa antes de desistir.
    O `wait_for_load_state` posterior é best-effort: DOM lento não pode matar o fluxo.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    ultimo_erro = None
    for tentativa in range(1, GOTO_TENTATIVAS + 1):
        try:
            page.goto(LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass  # DOM demorou; segue com o que já veio (screencast mostra o resto)
            return
        except PlaywrightTimeoutError as exc:
            ultimo_erro = exc
            logger.warning("Login ML: navegação estourou o tempo (tentativa %s/%s).",
                           tentativa, GOTO_TENTATIVAS)
    # Esgotou as tentativas: erro claro e acionável (não o TimeoutError cru do Playwright).
    raise RuntimeError(
        "O Mercado Livre demorou demais a responder a partir do servidor. "
        "Tente novamente; se persistir, é bloqueio do IP do servidor."
    ) from ultimo_erro


from apps.accounts.tenant import executar_no_tenant, organization_job_sem_transacao


def _gravar_sessao(user_id: int, storage_state: dict) -> None:
    """Grava a sessão capturada e derruba o cache da sonda. Roda no tenant, fora do loop."""
    from django.contrib.auth import get_user_model
    from apps.accounts.ml_sessions import save_storage_state
    from apps.scrapers.conexoes import invalidar_ml

    user = get_user_model().objects.get(pk=user_id)
    save_storage_state(user, storage_state)
    # A sonda de sessão é cacheada por 5 min; sem invalidar, quem acabou de conectar
    # continuaria vendo "desconectado" até o cache vencer — logo depois de fazer
    # exatamente o que pedimos. `invalidar_ml` é só cache.delete (não toca o banco),
    # então o try aqui é rede de segurança para Redis fora do ar. O `except: pass` que
    # existia escondia justamente esse sintoma.
    try:
        invalidar_ml(user)
    except Exception:
        logger.warning(
            "Sessão ML salva, mas a sonda cacheada não foi invalidada — a tela pode "
            "levar até 5 min para mostrar 'conectado' (user %s).", user_id, exc_info=True)


def _persistir_sessao(user_id: int, storage_state: dict, tentativas: int = 3) -> None:
    """Grava a sessão, tolerante a socket morto depois de minutos de browser ocioso."""
    from django.db import InterfaceError, OperationalError, close_old_connections

    for tentativa in range(1, tentativas + 1):
        try:
            executar_no_tenant(_gravar_sessao, user_id, storage_state)
            return
        except (OperationalError, InterfaceError) as exc:
            if tentativa == tentativas:
                raise
            logger.warning("Gravação da sessão ML falhou (%s/%s): %s",
                           tentativa, tentativas, exc)
            close_old_connections()
            time.sleep(0.5 * tentativa)


@organization_job_sem_transacao
def _worker(user_id: int):
    """Sobe o Chromium, publica capturas nítidas e valida a sessão de verdade."""
    from playwright.sync_api import sync_playwright
    from apps.scrapers.auxiliar import ua_aleatorio
    from apps.scrapers.conexoes import sondar_sessao_ml

    runtime = _transport.get(user_id) or _transport.create(user_id)

    estado_capturado = None
    validator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml-auth-probe")
    pending_validation = None
    pending_state = None
    last_fingerprint = ""
    manual_validation = False
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
            try:
                context = browser.new_context(
                    viewport={
                        "width": runtime.viewport["width"],
                        "height": runtime.viewport["height"],
                    },
                    device_scale_factor=runtime.viewport["device_pixel_ratio"],
                    is_mobile=runtime.viewport["device_class"] == "mobile",
                    has_touch=runtime.viewport["pointer"] == "coarse",
                    user_agent=ua_aleatorio(),
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.pages[0] if context.pages else context.new_page()
                active_page = ActivePage(context, page, runtime)

                _ir_para_login(page)
                _transport.capture(runtime, page, active=True)
                _set_estado(
                    user_id, fase="aguardando_login", erro="", aviso="",
                    session_id=runtime.session_id, viewport=runtime.viewport,
                )

                deadline = time.time() + LOGIN_DEADLINE_S
                logado = False
                last_beat = time.time()
                last_check = 0.0
                estado = {}
                while time.time() < deadline:
                    # O loop gira a cada 50ms para bombear os frames e drenar o input, mas
                    # os flags do usuário (cancelar / "já entrei") não mudam nessa escala:
                    # ler o cache 20x/s só gastava CPU. 500ms de latência num clique de
                    # cancelar ninguém percebe.
                    agora = time.time()
                    if agora - last_check > 0.5:
                        estado = cache.get(_cache_key(user_id)) or {}
                        last_check = agora
                    if estado.get("cancelar"):
                        _set_estado(user_id, fase="idle", erro="")
                        break

                    current_page = active_page.current()
                    if current_page is None:
                        raise RuntimeError("A janela de login do Mercado Livre foi fechada.")

                    # Drena e aplica os eventos confirmados pelo protocolo sequencial.
                    houve_input = False
                    for _ in range(MAX_EVENTOS_POR_POST * 4):
                        try:
                            ev = runtime.input_queue.get_nowait()
                        except queue.Empty:
                            break
                        despachar_input_v2(current_page, ev)
                        houve_input = True
                    _transport.capture(runtime, current_page, active=houve_input)

                    # Alterações de cookie disparam uma sonda assíncrona. A URL sozinha
                    # nunca representa sucesso: SMS, QR, ajuda e recuperação também
                    # navegam dentro de mercadolivre.com.br.
                    verificar_agora = bool(
                        estado.get("validar_agora") or estado.get("salvar_agora")
                    )
                    if verificar_agora:
                        manual_validation = True
                        _set_estado(
                            user_id, fase="validando", validar_agora=False,
                            salvar_agora=False, aviso="",
                        )

                    snapshot = context.storage_state()
                    fingerprint = _storage_fingerprint(snapshot)
                    changed = fingerprint != last_fingerprint
                    if changed:
                        last_fingerprint = fingerprint
                    if (
                        pending_validation is None
                        and (changed or manual_validation)
                    ):
                        pending_state = snapshot
                        pending_validation = validator.submit(
                            sondar_sessao_ml, pending_state,
                        )

                    if pending_validation is not None and pending_validation.done():
                        verdict, _reason = pending_validation.result()
                        pending_validation = None
                        logger.info(
                            "ml_login_metric transport=mercado_livre user=%s "
                            "validation=%s",
                            user_id, verdict,
                        )
                        if verdict == "conectado":
                            logado = True
                            estado_capturado = context.storage_state()
                            break
                        if manual_validation:
                            message = (
                                "Ainda não foi possível confirmar o login. "
                                "Conclua a etapa aberta no Mercado Livre e tente novamente."
                            )
                            if verdict == "inconclusivo":
                                message = (
                                    "O Mercado Livre demorou para confirmar a sessão. "
                                    "A janela continua aberta; tente verificar novamente."
                                )
                            _set_estado(
                                user_id, fase="aguardando_login", aviso=message,
                                erro="",
                            )
                            manual_validation = False

                    # Heartbeat: renova TTL + atualizado_em sem trocar de fase (o front
                    # segue desenhando as capturas pelo EventSource).
                    if time.time() - last_beat > 8:
                        current = cache.get(_cache_key(user_id)) or {}
                        if current.get("fase") != "validando":
                            _set_estado(user_id, fase="aguardando_login")
                        last_beat = time.time()

                    # Bombeia eventos do Playwright, inclusive popup/nova aba.
                    current_page.wait_for_timeout(LOOP_MS)

                if logado:
                    _set_estado(user_id, fase="salvando")
                    estado_capturado = estado_capturado or context.storage_state()
                elif (cache.get(_cache_key(user_id)) or {}).get("fase") != "idle":
                    _set_estado(user_id, fase="erro",
                                erro="Tempo esgotado esperando o login. Tente de novo.")
            finally:
                # Sem isto o Chromium ficava órfão em qualquer exceção: o close
                # estava solto no fim do bloco e simplesmente não era alcançado.
                try:
                    browser.close()
                except Exception:
                    logger.warning("Chromium do login ML não fechou limpo (user %s).",
                                   user_id, exc_info=True)

        # Fora do `with`: o loop do Playwright morreu, o ORM está liberado. A fase só
        # vira 'conectado' DEPOIS de a gravação confirmar — senão uma falha aqui
        # deixaria a tela presa em 'salvando' para sempre.
        if estado_capturado is not None:
            _persistir_sessao(user_id, estado_capturado)
            _set_estado(
                user_id, fase="conectado", salvar_agora=False,
                validar_agora=False, aviso="", erro="",
            )
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira mensagem pro usuário
        codigo = novo_codigo()
        logger.exception("Conexão ML falhou (user=%s codigo=%s)", user_id, codigo)
        _set_estado(user_id, fase="erro", codigo_erro=codigo,
                    erro=mensagem_de_erro(exc, codigo, servico="O Mercado Livre"))
    finally:
        validator.shutdown(wait=False, cancel_futures=True)
        _transport.finish(user_id, runtime)
        with _lock:
            _inputs.pop(user_id, None)
            _frames.pop(user_id, None)
            _threads.pop(user_id, None)


def criar_sessao(user, client: dict | None = None) -> dict:
    """Inicia (ou reaproveita) a sessão de login web do ML pro usuário."""
    from apps.accounts.feature_flags import enabled_for_user
    user_id = user.id
    if not enabled_for_user("ML_BROWSER_LOGIN_ENABLED", user):
        # GRAVA no cache em vez de só retornar: o front faz poll a cada 5s, e um
        # estado que não persiste é sobrescrito pelo `status()` seguinte — que lê
        # `fase='idle'` + `auth_valido=True` (a sessão antiga segue no banco) e
        # repinta "Conectado". Era exatamente o "Reconectar não faz nada, volta a
        # ficar conectado como se a sessão estivesse presa".
        return _set_estado(
            user_id, fase="indisponivel", cancelar=False, salvar_agora=False,
            erro="Login por navegador está desativado para esta organização.",
        )
    with _lock:
        viva = _threads.get(user_id)
        if viva and viva.is_alive():
            # Já tem sessão rolando neste worker — devolve o estado atual.
            return status(user_id)
        runtime = _transport.create(user_id, client)
        _set_estado(
            user_id, fase="iniciando", erro="", aviso="", cancelar=False,
            salvar_agora=False, validar_agora=False, session_id=runtime.session_id,
            viewport=runtime.viewport,
        )
        t = threading.Thread(target=_worker, args=(user_id,), daemon=True)
        _threads[user_id] = t
        t.start()
    return status(user_id)


def frames(user_id: int, session_id: str | None = None):
    """Eventos estruturados do transporte v2 para a view SSE."""
    yield from _transport.frames(user_id, session_id)


def enfileirar_input(user_id: int, session_id: str, eventos) -> dict:
    """Valida, deduplica e confirma eventos do transporte v2."""
    return _transport.enqueue(user_id, session_id, eventos)


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

    try:
        from django.contrib.auth import get_user_model
        from apps.accounts.ml_sessions import save_storage_state
        user = get_user_model().objects.get(pk=user_id)
        save_storage_state(user, dados)
    except Exception as exc:
        return _set_estado(user_id, fase="erro", erro=f"Não foi possível salvar a sessão: {exc}")

    return _set_estado(user_id, fase="conectado", erro="", salvar_agora=False, cancelar=False)


def salvar_agora(user_id: int):
    """Usuário pediu uma validação; nunca força a persistência de cookies inválidos."""
    _set_estado(user_id, validar_agora=True, salvar_agora=False, aviso="")


def cancelar(user_id: int):
    _set_estado(user_id, cancelar=True, fase="idle", aviso="", erro="")


def esquecer(user_id: int) -> None:
    """Zera o estado do login deste usuário.

    Sem isto, o "Desconectar" apagava a sessão no banco mas o poll seguinte ainda
    lia a fase antiga do cache — e a tela voltava a se pintar de verde por até 14
    minutos sobre uma sessão que já não existia.
    """
    cache.delete(_cache_key(user_id))
