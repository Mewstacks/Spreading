"""Live view para a sessão de relatórios da Amazon Associates.

É deliberadamente separado da tag/Creators API: aquelas dão links e catálogo;
esta sessão dá acesso ao portal de comissões. A senha é digitada na página real da
Amazon no Chromium remoto e nunca passa pelo Django.
"""
import queue
import threading
import time

from django.core.cache import cache

from apps.scrapers.ml_conexao import (
    GOTO_TIMEOUT_MS, LOGIN_DEADLINE_S, LOOP_MS, MAX_EVENTOS_POR_POST,
    SCREENCAST, VIEW_H, VIEW_W, _SPECIAL_KEYS, _despachar_input,
)
from apps.scrapers.report_sessions import has_report_session, save_report_state

LOGIN_URL = "https://associados.amazon.com.br/"
REPORT_URL = "https://associados.amazon.com.br/home/reports"
_threads, _frames, _inputs = {}, {}, {}
_lock = threading.Lock()


def _key(user_id):
    return f"amazon_report_conexao:{user_id}"


def _set(user_id, **values):
    state = cache.get(_key(user_id)) or {}
    state.update(values)
    state["atualizado_em"] = time.time()
    cache.set(_key(user_id), state, timeout=LOGIN_DEADLINE_S + 120)
    return state


def status(user_id):
    state = cache.get(_key(user_id)) or {"fase": "idle"}
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=user_id).first()
    state["auth_valido"] = bool(user and has_report_session(user, "amazon"))
    return state


def _logado(page) -> bool:
    """Só aceita uma rota interna autenticada, nunca a landing pública.

    A landing da Associates também usa o domínio ``associados.amazon.com.br``. A
    checagem antiga tratava essa página pública como login concluído e podia gravar
    cookies anônimos. A rota de relatórios exige autenticação e é a confirmação que
    interessa para este tipo de sessão.
    """
    value = (page.url or "").lower()
    if any(x in value for x in ("signin", "ap/signin", "login")):
        return False
    if "/home" not in value and "/reports" not in value:
        return False
    return page.locator("input[type='password'], input[name*='password' i]").count() == 0


def _worker(user):
    from playwright.sync_api import sync_playwright
    from apps.scrapers.auxiliar import ua_aleatorio

    uid = user.id
    fila = queue.Queue(maxsize=2000)
    with _lock:
        _inputs[uid] = fila
        _frames.pop(uid, None)
    try:
        _set(uid, fase="iniciando", erro="")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"])
            context = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, user_agent=ua_aleatorio())
            page = context.new_page()
            cdp = context.new_cdp_session(page)

            def on_frame(params):
                _frames[uid] = params.get("data", "")
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
                except Exception:
                    pass

            cdp.send("Page.enable")
            cdp.on("Page.screencastFrame", on_frame)
            try:
                cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
            except Exception:
                pass
            page.goto(LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
            cdp.send("Page.startScreencast", SCREENCAST)
            _set(uid, fase="aguardando_login", erro="")
            deadline, logged = time.time() + LOGIN_DEADLINE_S, False
            while time.time() < deadline:
                state = cache.get(_key(uid)) or {}
                if state.get("cancelar"):
                    _set(uid, fase="idle", erro="")
                    break
                for _ in range(MAX_EVENTOS_POR_POST * 4):
                    try:
                        _despachar_input(cdp, page, fila.get_nowait())
                    except queue.Empty:
                        break
                if state.get("salvar_agora"):
                    # O botão de salvar também valida a sessão na página que o
                    # sincronizador usará, sem depender da URL da landing.
                    page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                    _set(uid, salvar_agora=False)
                if _logado(page):
                    logged = True
                    break
                page.wait_for_timeout(LOOP_MS)
            if logged:
                _set(uid, fase="salvando")
                save_report_state(user, "amazon", context.storage_state())
                _set(uid, fase="conectado", erro="", salvar_agora=False)
            elif (cache.get(_key(uid)) or {}).get("fase") != "idle":
                _set(uid, fase="erro", erro="Tempo esgotado esperando o login da Amazon.")
            browser.close()
    except Exception as exc:
        _set(uid, fase="erro", erro=f"Falha na conexão Amazon: {exc}")
    finally:
        with _lock:
            _threads.pop(uid, None)
            _inputs.pop(uid, None)
            _frames.pop(uid, None)


def criar_sessao(user):
    with _lock:
        running = _threads.get(user.id)
        if running and running.is_alive():
            return status(user.id)
        _set(user.id, fase="iniciando", erro="", cancelar=False, salvar_agora=False)
        thread = threading.Thread(target=_worker, args=(user,), daemon=True)
        _threads[user.id] = thread
        thread.start()
    return status(user.id)


def frames(user_id):
    previous, waiting = None, 0
    while waiting < 600:
        if user_id not in _inputs:
            waiting += 1
            time.sleep(.05)
            continue
        frame = _frames.get(user_id)
        if frame and frame != previous:
            previous, waiting = frame, 0
            yield frame
        else:
            waiting += 1
        time.sleep(.05)


def enfileirar_input(user_id, eventos):
    queue_ = _inputs.get(user_id)
    if queue_ is None:
        return {"ok": False, "erro": "sessao_inativa"}
    if not isinstance(eventos, list):
        return {"ok": False, "erro": "payload_invalido"}
    accepted = 0
    for event in eventos[:MAX_EVENTOS_POR_POST]:
        if not isinstance(event, dict):
            continue
        kind = event.get("t")
        clean = {"t": kind}
        if kind in {"move", "down", "up", "wheel"}:
            try:
                clean["x"] = max(0, min(VIEW_W, int(event.get("x", 0))))
                clean["y"] = max(0, min(VIEW_H, int(event.get("y", 0))))
            except (TypeError, ValueError):
                continue
            if kind in {"down", "up"}:
                clean["button"] = event.get("button") if event.get("button") in {"left", "right", "middle"} else "left"
                clean["clickCount"] = event.get("clickCount", 1)
            elif kind == "wheel":
                clean["dx"] = event.get("dx", 0)
                clean["dy"] = event.get("dy", 0)
            else:
                clean["buttons"] = event.get("buttons", 0)
        elif kind == "char":
            clean["text"] = str(event.get("text", ""))[:8]
            if not clean["text"]:
                continue
        elif kind == "key":
            if event.get("key") not in _SPECIAL_KEYS:
                continue
            clean["key"] = event["key"]
        else:
            continue
        try:
            queue_.put_nowait(clean)
            accepted += 1
        except queue.Full:
            break
    return {"ok": True, "aceitos": accepted}


def salvar_agora(user_id):
    _set(user_id, salvar_agora=True)


def cancelar(user_id):
    _set(user_id, cancelar=True)
