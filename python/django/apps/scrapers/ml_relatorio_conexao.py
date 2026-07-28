"""Live view para a sessão de RELATÓRIOS do Mercado Livre (portal de afiliados).

Espelha amazon_conexao.py e é deliberadamente separado de ml_conexao.py:
  - ml_conexao  -> sessão do SITE PRINCIPAL (Link Builder, geração de link).
  - este módulo -> sessão do PORTAL DE AFILIADOS (métricas/comissão).

O portal de afiliados tem SSO próprio ("jms/msl"): mesmo com cookies válidos no
site principal ele pode exigir novo login. Reusar a sessão principal para ler
relatório era a causa do loop "reconecte o ML" que nunca se resolvia — reconectar
o site principal não tocava na sessão que o relatório de fato precisa.

Os eventos de entrada são retransmitidos ao Chromium e nunca são persistidos ou
registrados em logs. O storage_state é salvo cifrado via report_sessions.
"""
import logging
import queue
import threading
import time
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

from apps.scrapers.erros_conexao import mensagem_de_erro, novo_codigo
from apps.scrapers.ml_conexao import (
    GOTO_TIMEOUT_MS, LOGIN_DEADLINE_S, LOOP_MS, MAX_EVENTOS_POR_POST,
)
from apps.scrapers.ml_live_transport import (
    ActivePage, LiveTransport, despachar_input,
)
from apps.scrapers.report_sessions import has_report_session, save_report_state
from apps.accounts.tenant import organization_job_sem_transacao

logger = logging.getLogger(__name__)

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
_transport = LiveTransport("mercado_livre_relatorios")


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
    state.update(_transport.status(user_id))
    return state


def _logado(page) -> bool:
    """Aceita só uma rota autenticada do portal de afiliados, nunca signin.

    A landing /afiliados/ é pública; por isso a validação real acontece ao navegar
    para a página de relatório (_report_url) e confirmar a ausência de campo de
    senha — o mesmo cuidado do fluxo Amazon."""
    try:
        value = (page.url or "").lower()
        if any(x in value for x in ("signin", "/login", "lgz", "loginhub", "msl/login")):
            return False
        expected = urlparse(_report_url())
        current = urlparse(value)
        if current.netloc != expected.netloc.lower():
            return False
        expected_path = expected.path.rstrip("/")
        if not current.path.rstrip("/").startswith(expected_path):
            return False
        return page.locator("input[type='password'], input[name*='password' i]").count() == 0
    except Exception:
        # Consulta ao DOM durante uma navegação levanta; aqui isso significa
        # "ainda não confirmado", e não pode derrubar o worker.
        return False


# Sem transação: organization_job envolveria os até 10 min do live view num
# transaction.atomic(), deixando uma conexão `idle in transaction` enquanto o
# usuário digita a senha.
@organization_job_sem_transacao
def _worker(user):
    from playwright.sync_api import sync_playwright
    from apps.scrapers.auxiliar import ua_aleatorio

    uid = user.id
    runtime = _transport.get(uid) or _transport.create(uid)
    estado_capturado = None
    try:
        _set(uid, fase="iniciando", erro="")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"])
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
                )
                page = context.new_page()
                active_page = ActivePage(context, page, runtime)
                # Abrir o destino autenticado preserva o redirect de retorno do SSO:
                # depois do SMS/QR, o ML volta para a página que podemos validar.
                page.goto(
                    _report_url(), wait_until="domcontentloaded",
                    timeout=GOTO_TIMEOUT_MS,
                )
                _transport.capture(runtime, page, active=True)
                _set(
                    uid, fase="aguardando_login", erro="", aviso="",
                    session_id=runtime.session_id, viewport=runtime.viewport,
                )
                deadline, logged = time.time() + LOGIN_DEADLINE_S, False
                while time.time() < deadline:
                    state = cache.get(_key(uid)) or {}
                    if state.get("cancelar"):
                        _set(uid, fase="idle", erro="")
                        break
                    current_page = active_page.current()
                    if current_page is None:
                        raise RuntimeError("A janela do portal de afiliados foi fechada.")
                    had_input = False
                    for _ in range(MAX_EVENTOS_POR_POST * 4):
                        try:
                            event = runtime.input_queue.get_nowait()
                        except queue.Empty:
                            break
                        despachar_input(current_page, event)
                        had_input = True
                    _transport.capture(runtime, current_page, active=had_input)

                    validate_now = bool(
                        state.get("validar_agora") or state.get("salvar_agora")
                    )
                    if validate_now:
                        _set(
                            uid, fase="validando", validar_agora=False,
                            salvar_agora=False, aviso="",
                        )
                        current_page.goto(
                            _report_url(), wait_until="domcontentloaded",
                            timeout=GOTO_TIMEOUT_MS,
                        )
                        _transport.capture(runtime, current_page, active=True)
                    authenticated = _logado(current_page)
                    if validate_now or authenticated:
                        logger.info(
                            "ml_login_metric transport=mercado_livre_relatorios "
                            "user=%s validation=%s",
                            uid, "conectado" if authenticated else "expirado",
                        )
                    if authenticated:
                        logged = True
                        break
                    if validate_now:
                        _set(
                            uid, fase="aguardando_login", erro="",
                            aviso=(
                                "O portal ainda pede autenticação. Conclua a etapa "
                                "aberta no Mercado Livre e verifique novamente."
                            ),
                        )
                    current_page.wait_for_timeout(LOOP_MS)
                if logged:
                    _set(uid, fase="salvando")
                    # Só a leitura fica aqui. Gravar depois do bloco mantém o mesmo
                    # formato dos outros workers e garante que a fase só vire
                    # 'conectado' quando a sessão estiver de fato no disco.
                    estado_capturado = context.storage_state()
                elif (cache.get(_key(uid)) or {}).get("fase") != "idle":
                    _set(uid, fase="erro", erro="Tempo esgotado esperando o login do portal de afiliados.")
            finally:
                # Em finally: solto no fim do bloco, o close não era alcançado
                # em nenhuma exceção e o Chromium ficava órfão.
                try:
                    browser.close()
                except Exception:
                    logger.warning("Chromium do login do portal de afiliados ML não fechou "
                                   "limpo (user %s).", uid, exc_info=True)

        if estado_capturado is not None:
            save_report_state(user, "mercadolivre", estado_capturado)
            _set(
                uid, fase="conectado", erro="", aviso="",
                salvar_agora=False, validar_agora=False,
            )
    except Exception as exc:
        codigo = novo_codigo()
        logger.exception("Conexão do portal de afiliados ML falhou (user=%s codigo=%s)",
                         uid, codigo)
        _set(uid, fase="erro", codigo_erro=codigo,
             erro=mensagem_de_erro(exc, codigo,
                                   servico="O portal de afiliados do Mercado Livre"))
    finally:
        _transport.finish(uid, runtime)
        with _lock:
            _threads.pop(uid, None)
            _inputs.pop(uid, None)
            _frames.pop(uid, None)


def criar_sessao(user, client: dict | None = None):
    from apps.accounts.feature_flags import enabled_for_user
    if not enabled_for_user("ML_BROWSER_REPORTS_ENABLED", user):
        # Persistido, e não só retornado: o poll de 5s do front relê o cache e um
        # estado efêmero seria imediatamente apagado pela fase antiga (ver a mesma
        # correção em ml_conexao.criar_sessao).
        return {
            **_set(user.id, fase="indisponivel", cancelar=False, salvar_agora=False,
                   erro="Login do portal de relatórios está desativado para esta organização."),
            "auth_valido": False,
        }
    with _lock:
        running = _threads.get(user.id)
        if running and running.is_alive():
            return status(user.id)
        runtime = _transport.create(user.id, client)
        _set(
            user.id, fase="iniciando", erro="", aviso="", cancelar=False,
            salvar_agora=False, validar_agora=False, session_id=runtime.session_id,
            viewport=runtime.viewport,
        )
        thread = threading.Thread(target=_worker, args=(user,), daemon=True)
        _threads[user.id] = thread
        thread.start()
    return status(user.id)


def frames(user_id, session_id=None):
    yield from _transport.frames(user_id, session_id)


def enfileirar_input(user_id, session_id, eventos):
    return _transport.enqueue(user_id, session_id, eventos)


def salvar_agora(user_id):
    _set(user_id, validar_agora=True, salvar_agora=False, aviso="")


def cancelar(user_id):
    _set(user_id, cancelar=True, fase="idle", aviso="", erro="")
