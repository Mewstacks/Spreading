"""Live view para a sessão de RELATÓRIOS do Mercado Livre (portal de afiliados).

Espelha amazon_conexao.py e é deliberadamente separado de ml_conexao.py:
  - ml_conexao  -> sessão do SITE PRINCIPAL (Link Builder, geração de link).
  - este módulo -> sessão do PORTAL DE AFILIADOS (métricas/comissão).

O portal de afiliados tem SSO próprio ("jms/msl"): mesmo com cookies válidos no
site principal ele pode exigir novo login. Reusar a sessão principal para ler
relatório era a causa do loop "reconecte o ML" que nunca se resolvia — reconectar
o site principal não tocava na sessão que o relatório de fato precisa.

A senha é digitada na página REAL do ML dentro do Chromium remoto e nunca passa
pelo Django. O storage_state é salvo cifrado via report_sessions.save_report_state.
"""
import queue
import threading
import time

from django.conf import settings
from django.core.cache import cache

from apps.scrapers.ml_conexao import (
    GOTO_TIMEOUT_MS, LOGIN_DEADLINE_S, LOOP_MS, MAX_EVENTOS_POR_POST,
    SCREENCAST, VIEW_H, VIEW_W, _SPECIAL_KEYS, _despachar_input,
)
from apps.scrapers.report_sessions import has_report_session, save_report_state

# Portal de afiliados. O usuário loga aqui dentro do live view.
LOGIN_URL = "https://www.mercadolivre.com.br/afiliados/"


def _report_url() -> str:
    """URL da página de métricas/relatório do portal de afiliados.

    Configurável pelo secret ML_AFFILIATE_REPORT_URL (o adapter lê a mesma). Sem
    ela, valida a sessão no hub autenticado do Link Builder."""
    return (getattr(settings, "ML_AFFILIATE_REPORT_URL", "") or "").strip() or \
        "https://www.mercadolivre.com.br/afiliados/linkbuilder#hub"


_threads, _frames, _inputs = {}, {}, {}
_lock = threading.Lock()


def _key(user_id):
    return f"ml_report_conexao:{user_id}"


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
    state["auth_valido"] = bool(user and has_report_session(user, "mercadolivre"))
    return state


def _logado(page) -> bool:
    """Aceita só uma rota autenticada do portal de afiliados, nunca signin.

    A landing /afiliados/ é pública; por isso a validação real acontece ao navegar
    para a página de relatório (_report_url) e confirmar a ausência de campo de
    senha — o mesmo cuidado do fluxo Amazon."""
    value = (page.url or "").lower()
    if any(x in value for x in ("signin", "/login", "lgz", "loginhub", "msl/login")):
        return False
    if "/afiliados/" not in value:
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
                    # O botão "Já entrei" valida a sessão na página que o
                    # sincronizador vai usar, sem depender da URL da landing.
                    page.goto(_report_url(), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                    _set(uid, salvar_agora=False)
                if _logado(page):
                    logged = True
                    break
                page.wait_for_timeout(LOOP_MS)
            if logged:
                _set(uid, fase="salvando")
                save_report_state(user, "mercadolivre", context.storage_state())
                _set(uid, fase="conectado", erro="", salvar_agora=False)
            elif (cache.get(_key(uid)) or {}).get("fase") != "idle":
                _set(uid, fase="erro", erro="Tempo esgotado esperando o login do portal de afiliados.")
            browser.close()
    except Exception as exc:
        _set(uid, fase="erro", erro=f"Falha na conexão do portal de afiliados ML: {exc}")
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
