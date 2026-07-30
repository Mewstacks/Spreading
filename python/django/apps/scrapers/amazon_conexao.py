"""Live view para a sessão de relatórios da Amazon Associates.

É deliberadamente separado da tag/Creators API: aquelas dão links e catálogo;
esta sessão dá acesso ao portal de comissões. A senha é digitada na página real da
Amazon no Chromium remoto e nunca passa pelo Django.

Usa o mesmo `ml_live_transport` dos dois fluxos do ML. Até a migração este módulo era
o último em `Page.startScreencast` do CDP, e a diferença aparecia na tela: JPEG de
qualidade 55 reduzido a 960x600 e esticado de volta, sem seq nos frames (dois frames
seguidos recebiam o mesmo número e o mais novo era descartado, então a imagem
congelava e saltava), sem heartbeat, sem viewport de celular e sem kill-switch.
"""
import logging
import queue
import threading
import time

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

LOGIN_URL = "https://associados.amazon.com.br/"
REPORT_URL = "https://associados.amazon.com.br/home/reports"

_threads = {}
_lock = threading.Lock()
_transport = LiveTransport("amazon_relatorios")

GOTO_TENTATIVAS = 2


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
    state.update(_transport.status(user_id))
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


def _abrir_login(page):
    """Navega até o login com uma 2ª tentativa — o mesmo tratamento do ML.

    Do IP de datacenter da Fly a Amazon também passa por gateway anti-bot e a
    primeira navegação estoura o tempo com frequência. O ML já tratava isso
    (ml_conexao._ir_para_login); aqui um único timeout matava a tentativa de login
    inteira e o usuário via só "Tempo esgotado", sem nada a fazer além de repetir.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    for tentativa in range(1, GOTO_TENTATIVAS + 1):
        try:
            page.goto(LOGIN_URL, wait_until="commit", timeout=GOTO_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            logger.warning("Login Amazon: navegação estourou o tempo (tentativa %s/%s).",
                           tentativa, GOTO_TENTATIVAS)
    raise RuntimeError(
        "A Amazon demorou demais a responder a partir do servidor. "
        "Tente novamente em alguns instantes."
    )


# Sem transação: organization_job envolveria os até 10 min do live view num
# transaction.atomic(), deixando uma conexão `idle in transaction` enquanto o
# usuário digita a senha.
@organization_job_sem_transacao
def _worker(user):
    from playwright.sync_api import sync_playwright
    from apps.scrapers.contexto_login import (
        LAUNCH_ARGS, habilitar_foco, opcoes_de_contexto,
    )

    uid = user.id
    runtime = _transport.get(uid) or _transport.create(uid)
    estado_capturado = None
    try:
        _set(uid, fase="iniciando", erro="")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                context = browser.new_context(
                    **opcoes_de_contexto(browser, runtime.viewport),
                )
                page = context.pages[0] if context.pages else context.new_page()
                habilitar_foco(context, page)
                active_page = ActivePage(context, page, runtime)

                _abrir_login(page)
                _transport.capture(runtime, page, active=True)
                _set(
                    uid, fase="aguardando_login", erro="", aviso="",
                    session_id=runtime.session_id, viewport=runtime.viewport,
                )
                deadline, logged = time.time() + LOGIN_DEADLINE_S, False
                # O laço gira a 20Hz (LOOP_MS=50) para drenar input e publicar frames,
                # mas nem o cache nem o DOM mudam nessa escala. Ler os dois a cada volta
                # custava, POR SEGUNDO, 20 idas ao cache e 20 consultas de seletor via
                # CDP — dentro do processo do gunicorn, segurando a GIL e derrubando a
                # latência das outras threads. Meio segundo de latência num clique de
                # cancelar ninguém percebe.
                state = {}
                proxima_leitura = 0.0
                proxima_checagem = 0.0
                while time.time() < deadline:
                    agora = time.time()
                    if agora >= proxima_leitura:
                        state = cache.get(_key(uid)) or {}
                        proxima_leitura = agora + 0.5
                    if state.get("cancelar"):
                        _set(uid, fase="idle", erro="")
                        break

                    current_page = active_page.current()
                    if current_page is None:
                        raise RuntimeError("A janela de login da Amazon foi fechada.")

                    had_input = False
                    for _ in range(MAX_EVENTOS_POR_POST * 4):
                        try:
                            event = runtime.input_queue.get_nowait()
                        except queue.Empty:
                            break
                        despachar_input(current_page, event)
                        had_input = True
                    _transport.capture(runtime, current_page, active=had_input)

                    if state.get("salvar_agora") or state.get("validar_agora"):
                        # O botão de salvar também valida a sessão na página que o
                        # sincronizador usará, sem depender da URL da landing.
                        _set(uid, fase="validando", salvar_agora=False,
                             validar_agora=False, aviso="")
                        # Zera a cópia local: o cache só é relido a cada 0,5s e sem isto
                        # o pedido seguiria "ligado", repetindo o goto a cada volta.
                        state["salvar_agora"] = False
                        state["validar_agora"] = False
                        current_page.goto(REPORT_URL, wait_until="domcontentloaded",
                                          timeout=GOTO_TIMEOUT_MS)
                        _transport.capture(runtime, current_page, active=True)
                        proxima_checagem = 0.0  # navegou: confere já

                    if agora < proxima_checagem:
                        current_page.wait_for_timeout(LOOP_MS)
                        continue
                    proxima_checagem = agora + 1.0
                    if _logado(current_page):
                        logged = True
                        break
                    current_page.wait_for_timeout(LOOP_MS)
                if logged:
                    _set(uid, fase="salvando")
                    # Só a leitura fica aqui. Gravar depois do bloco mantém o mesmo
                    # formato dos outros workers e garante que a fase só vire
                    # 'conectado' quando a sessão estiver de fato no disco.
                    estado_capturado = context.storage_state()
                elif (cache.get(_key(uid)) or {}).get("fase") != "idle":
                    _set(uid, fase="erro", erro="Tempo esgotado esperando o login da Amazon.")
            finally:
                # Em finally: solto no fim do bloco, o close nao era alcancado
                # em nenhuma excecao e o Chromium ficava orfao.
                try:
                    browser.close()
                except Exception:
                    logger.warning("Chromium do login Amazon não fechou limpo (user %s).",
                                   uid, exc_info=True)

        if estado_capturado is not None:
            save_report_state(user, "amazon", estado_capturado)
            _set(uid, fase="conectado", erro="", aviso="",
                 salvar_agora=False, validar_agora=False)
    except Exception as exc:
        codigo = novo_codigo()
        logger.exception("Conexão Amazon falhou (user=%s codigo=%s)", uid, codigo)
        _set(uid, fase="erro", codigo_erro=codigo,
             erro=mensagem_de_erro(exc, codigo, servico="A Amazon"))
    finally:
        _transport.finish(uid, runtime)
        with _lock:
            _threads.pop(uid, None)


def criar_sessao(user, client: dict | None = None):
    from apps.accounts.feature_flags import enabled_for_user
    if not enabled_for_user("AMAZON_BROWSER_LOGIN_ENABLED", user):
        # Persistido, e não só retornado: o poll do front relê o cache e um estado
        # efêmero seria imediatamente apagado pela fase antiga — a tela repintava
        # "Conectado" sobre uma sessão que nunca abriu (ver a mesma correção em
        # ml_conexao.criar_sessao).
        return {
            **_set(user.id, fase="indisponivel", cancelar=False, salvar_agora=False,
                   erro="Login da Amazon por navegador está desativado para esta organização."),
            "auth_valido": False,
        }
    with _lock:
        running = _threads.get(user.id)
        if running and running.is_alive():
            return status(user.id)
        runtime = _transport.create(user.id, client)
        _set(user.id, fase="iniciando", erro="", aviso="", cancelar=False,
             salvar_agora=False, validar_agora=False,
             session_id=runtime.session_id, viewport=runtime.viewport)
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
