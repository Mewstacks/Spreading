"""Conexão web do Mercado Livre — login sem script local, sem colar auth.json.

Substitui a gambiarra de "rode connect_ml.py no seu PC e cole o auth.json". Num
servidor headless não dá pra abrir um browser pro usuário clicar, então rodamos o
Chromium num serviço de browser hospedado (Browserbase) e transmitimos a tela pro
navegador do usuário via *live view* (um iframe). Ele loga no ML ali dentro — no
celular ou no desktop — e quando a sessão fica válida capturamos o storage_state
e salvamos no mesmo `auth_{id}.json` que o resto do scraper já espera.

Fluxo (espelha o QR do WhatsApp):
  1. criar_sessao(user)  -> abre sessão remota, navega pro login do ML, guarda o
     live_view_url no cache e dispara uma thread que fica observando o login.
  2. front embute o live_view_url num <iframe> e faz polling em status().
  3. thread detecta o redirect pós-login -> salva auth_{id}.json -> fase 'conectado'.

Estado compartilhado vai pro cache (Redis em prod) pra funcionar entre workers do
gunicorn; a thread que segura a conexão CDP vive em um worker só, mas escreve o
progresso no cache que qualquer worker lê no polling.
"""
import os
import threading
import time

from django.conf import settings
from django.core.cache import cache

LOGIN_URL = "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/"
HOME_HOST = "mercadolivre.com.br"

SESSION_TIMEOUT_S = 900          # tempo máx. da sessão remota (Browserbase)
LOGIN_DEADLINE_S = 600           # tempo máx. esperando o usuário logar
POLL_INTERVAL_S = 2

# Threads ativas por usuário (dentro deste worker). O cache guarda o estado
# visível entre workers; este dict só evita abrir 2 sessões no mesmo worker.
_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()


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


def _config_ok() -> tuple[bool, str]:
    if not getattr(settings, "BROWSERBASE_API_KEY", ""):
        return False, "BROWSERBASE_API_KEY não configurada no servidor."
    if not getattr(settings, "BROWSERBASE_PROJECT_ID", ""):
        return False, "BROWSERBASE_PROJECT_ID não configurada no servidor."
    return True, ""


def _abrir_sessao_remota():
    """Cria a sessão no Browserbase e devolve (connect_url, live_view_url, session_id)."""
    from browserbase import Browserbase

    bb = Browserbase(api_key=settings.BROWSERBASE_API_KEY)

    kwargs = dict(
        project_id=settings.BROWSERBASE_PROJECT_ID,
        keep_alive=True,
        api_timeout=SESSION_TIMEOUT_S,  # duração da sessão remota (não é o HTTP timeout)
    )
    # Proxy residencial no país do usuário reduz o bloqueio anti-bot do ML no login,
    # MAS é recurso de plano PAGO do Browserbase (o free plan responde 402). Só liga
    # com BROWSERBASE_USE_PROXY=1; sem ele o login roda no plano grátis (IP do datacenter).
    if getattr(settings, "BROWSERBASE_USE_PROXY", False):
        pais = getattr(settings, "BROWSERBASE_PROXY_COUNTRY", "BR") or "BR"
        kwargs["proxies"] = [{"type": "browserbase", "geolocation": {"country": pais}}]

    sessao = bb.sessions.create(**kwargs)
    live = bb.sessions.debug(sessao.id)
    live_view_url = getattr(live, "debugger_fullscreen_url", None) or getattr(
        live, "debuggerFullscreenUrl", None
    )
    return bb, sessao.id, sessao.connect_url, live_view_url


def _liberar_sessao(bb, session_id: str):
    try:
        bb.sessions.update(
            session_id, project_id=settings.BROWSERBASE_PROJECT_ID, status="REQUEST_RELEASE"
        )
    except Exception:
        pass


def _url_logada(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    if "login" in u or "/jms/" in u or "hub.mercadolibre" in u:
        return False
    return HOME_HOST in u or "mercadolibre.com" in u


def _worker(user_id: int):
    """Segura a conexão CDP, navega pro login e espera o usuário concluir."""
    from playwright.sync_api import sync_playwright

    bb = session_id = None
    try:
        ok, msg = _config_ok()
        if not ok:
            _set_estado(user_id, fase="erro", erro=msg)
            return

        _set_estado(user_id, fase="iniciando", erro="", live_view_url="")
        bb, session_id, connect_url, live_view_url = _abrir_sessao_remota()
        if not live_view_url:
            _set_estado(user_id, fase="erro", erro="Serviço não retornou o live view.")
            _liberar_sessao(bb, session_id)
            return

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(connect_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            _set_estado(
                user_id,
                fase="aguardando_login",
                live_view_url=live_view_url,
                session_id=session_id,
            )

            deadline = time.time() + LOGIN_DEADLINE_S
            logado = False
            while time.time() < deadline:
                estado = cache.get(_cache_key(user_id)) or {}
                if estado.get("cancelar"):
                    _set_estado(user_id, fase="idle", erro="")
                    break
                try:
                    url_atual = page.evaluate("() => location.href")
                except Exception:
                    url_atual = page.url
                # 'salvar_agora' = usuário clicou "já entrei"; força a captura.
                if _url_logada(url_atual) or estado.get("salvar_agora"):
                    logado = True
                    break
                time.sleep(POLL_INTERVAL_S)

            if logado:
                _set_estado(user_id, fase="salvando")
                context.storage_state(path=_auth_path(user_id))
                _set_estado(user_id, fase="conectado", live_view_url="", salvar_agora=False)
            elif (cache.get(_cache_key(user_id)) or {}).get("fase") != "idle":
                _set_estado(
                    user_id,
                    fase="erro",
                    erro="Tempo esgotado esperando o login. Tente de novo.",
                )
            browser.close()
    except Exception as exc:  # noqa: BLE001 — qualquer falha vira mensagem pro usuário
        _set_estado(user_id, fase="erro", erro=f"Falha na conexão: {exc}")
    finally:
        if bb and session_id:
            _liberar_sessao(bb, session_id)
        with _lock:
            _threads.pop(user_id, None)


def criar_sessao(user) -> dict:
    """Inicia (ou reaproveita) a sessão de login web do ML pro usuário."""
    user_id = user.id
    ok, msg = _config_ok()
    if not ok:
        return _set_estado(user_id, fase="erro", erro=msg)

    with _lock:
        viva = _threads.get(user_id)
        if viva and viva.is_alive():
            # Já tem sessão rolando neste worker — devolve o estado atual.
            return status(user_id)
        _set_estado(user_id, fase="iniciando", erro="", live_view_url="", cancelar=False,
                    salvar_agora=False)
        t = threading.Thread(target=_worker, args=(user_id,), daemon=True)
        _threads[user_id] = t
        t.start()
    return status(user_id)


def salvar_agora(user_id: int):
    """Usuário clicou 'já entrei' — pede pra thread capturar a sessão agora."""
    _set_estado(user_id, salvar_agora=True)


def cancelar(user_id: int):
    _set_estado(user_id, cancelar=True)
