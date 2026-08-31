"""Live view para sessões Amazon Associates e da conta de compras.

É deliberadamente separado da tag/Creators API: aquelas dão links e catálogo;
esta sessão dá acesso ao portal de comissões. A senha é digitada na página real da
Amazon no Chromium remoto e nunca passa pelo Django.

Usa o mesmo LiveTransport dos dois fluxos do ML. O transporte antigo (CDP
Page.startScreencast) só emitia quadro quando havia mudança visual: medido em
homologação, uma tela de login parada produzia ZERO frames em 40s. Isso disparava
o watchdog do cliente — o overlay "Reconectando" cobria justamente o CAPTCHA que
o usuário precisava ler — e encerrava o stream, matando os listeners de teclado.
O LiveTransport captura por screenshot em cadência própria e manda heartbeat.

Os eventos de entrada são retransmitidos ao Chromium e nunca são persistidos ou
registrados em logs. O storage_state é salvo cifrado via report_sessions.
"""
import logging
import queue
import threading
import time
from urllib.parse import urlsplit

from django.core.cache import cache

from apps.scrapers.erros_conexao import mensagem_de_erro, novo_codigo
from apps.scrapers.ml_conexao import (
    GOTO_TENTATIVAS, GOTO_TIMEOUT_MS, LOGIN_DEADLINE_S, MAX_EVENTOS_POR_POST,
)
from apps.scrapers.ml_live_transport import (
    ActivePage, LiveTransport, despachar_input, interactive_browser_slot,
    aguardar_atividade,
    pode_inspecionar,
)
from apps.scrapers.report_sessions import has_report_session, save_report_state
from apps.accounts.tenant import organization_job_sem_transacao

logger = logging.getLogger(__name__)

LOGIN_URL = "https://associados.amazon.com.br/"
REPORT_URL = "https://associados.amazon.com.br/home/reports"
SHOP_URL = "https://www.amazon.com.br/gp/css/homepage.html"
SHOPEE_PROFILE_URL = "https://shopee.com.br/user/account/profile"
_threads = {}
_lock = threading.Lock()
_transport = LiveTransport("amazon_associados")
_shop_transport = LiveTransport("amazon_loja")
_shopee_transport = LiveTransport("shopee_loja")


def _is_shopee(shopper):
    return shopper == "shopee"


def _store_name(shopper):
    return "Shopee" if _is_shopee(shopper) else "Amazon"


def _key(user_id, shopper=False):
    if _is_shopee(shopper):
        return f"shopee_shop_conexao:{user_id}"
    tipo = "shop" if shopper else "report"
    return f"amazon_{tipo}_conexao:{user_id}"


def _live_transport(shopper=False):
    if _is_shopee(shopper):
        return _shopee_transport
    return _shop_transport if shopper else _transport


def _session_provider(shopper=False):
    if _is_shopee(shopper):
        return "shopee_shop"
    return "amazon_shop" if shopper else "amazon"


def _set(user_id, *, shopper=False, **values):
    state = cache.get(_key(user_id, shopper)) or {}
    state.update(values)
    state["atualizado_em"] = time.time()
    cache.set(_key(user_id, shopper), state, timeout=LOGIN_DEADLINE_S + 120)
    if any(values.get(command) for command in ("cancelar", "salvar_agora")):
        _live_transport(shopper).wake(user_id)
    return state


def status(user_id, *, shopper=False):
    state = cache.get(_key(user_id, shopper)) or {"fase": "idle"}
    from django.contrib.auth import get_user_model
    user = get_user_model().objects.filter(pk=user_id).first()
    state["auth_valido"] = bool(
        user and has_report_session(user, _session_provider(shopper))
    )
    # Sem isto a tela nunca se autocorrigia: o poll não recebia session_id,
    # viewport nem estado do stream, então um badge errado ficava errado.
    state.update(_live_transport(shopper).status(user_id))
    return state


def _logado(page, *, shopper=False) -> bool:
    """Só aceita uma rota interna autenticada, nunca a landing pública.

    A landing da Associates também usa o domínio ``associados.amazon.com.br``. A
    checagem antiga tratava essa página pública como login concluído e podia gravar
    cookies anônimos. A rota de relatórios exige autenticação e é a confirmação que
    interessa para este tipo de sessão.
    """
    try:
        value = (page.url or "").lower()
        if any(x in value for x in ("signin", "ap/signin", "login")):
            return False
        if _is_shopee(shopper):
            host = urlsplit(value).hostname or ""
            return (
                (host == "shopee.com.br" or host.endswith(".shopee.com.br"))
                and "/user/account" in value
                and "/verify/traffic" not in value
                and page.locator(
                    "input[type='password'], input[name*='password' i]",
                ).count() == 0
            )
        if shopper:
            host = urlsplit(value).hostname or ""
            return (
                (host == "amazon.com.br" or host.endswith(".amazon.com.br"))
                and "/gp/css/homepage" in value
                and page.locator(
                    "input[type='password'], input[name*='password' i]",
                ).count() == 0
            )
        if "/home" not in value and "/reports" not in value:
            return False
        return page.locator("input[type='password'], input[name*='password' i]").count() == 0
    except Exception:
        # Consultar o DOM no meio de uma navegação levanta ("Execution context was
        # destroyed"). Isso é "ainda não confirmado", não "a sessão morreu" — sem
        # esta guarda a exceção subiria e o finally fecharia o Chromium.
        return False


def _abrir_login(page, *, shopper=False):
    """Navega até o login com uma 2ª tentativa — o mesmo tratamento do ML.

    Do IP de datacenter da Fly a Amazon também passa por gateway anti-bot e a
    primeira navegação estoura o tempo com frequência. O ML já tratava isso
    (ml_conexao._ir_para_login); aqui um único timeout matava a tentativa de login
    inteira e o usuário via só "Tempo esgotado", sem nada a fazer além de repetir.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    for tentativa in range(1, GOTO_TENTATIVAS + 1):
        try:
            page.goto(
                (SHOPEE_PROFILE_URL if _is_shopee(shopper)
                 else SHOP_URL if shopper else LOGIN_URL),
                wait_until="commit", timeout=GOTO_TIMEOUT_MS,
            )
            return
        except PlaywrightTimeoutError:
            logger.warning("Login %s: navegação estourou o tempo (tentativa %s/%s).",
                           _store_name(shopper), tentativa, GOTO_TENTATIVAS)
    raise RuntimeError(
        f"A {_store_name(shopper)} demorou demais a responder a partir do servidor. "
        "Tente novamente em alguns instantes."
    )


# Sem transação: organization_job envolveria os até 10 min do live view num
# transaction.atomic(), deixando uma conexão `idle in transaction` enquanto o
# usuário digita a senha.
@organization_job_sem_transacao
def _worker(user, shopper=False):
    from playwright.sync_api import sync_playwright
    from apps.scrapers.contexto_login import (
        LAUNCH_ARGS, habilitar_foco, opcoes_de_contexto,
    )

    uid = user.id
    transport = _live_transport(shopper)
    runtime = transport.get(uid) or transport.create(uid)
    estado_capturado = None
    try:
        _set(uid, shopper=shopper, fase="iniciando", erro="")
        with interactive_browser_slot(), sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                context = browser.new_context(**opcoes_de_contexto(
                    browser, runtime.viewport,
                    # Colar a senha é o caminho normal de quem usa gerenciador de
                    # senhas; sem a permissão o Ctrl+V não funcionava.
                    permissions=["clipboard-read", "clipboard-write"],
                ))
                page = context.new_page()
                habilitar_foco(context, page)
                # Desafios da Amazon costumam abrir popup/nova aba. Sem seguir a
                # aba ativa, o stream ficava preso na página antiga.
                active_page = ActivePage(context, page, runtime)
                _abrir_login(page, shopper=shopper)
                transport.capture(runtime, page, active=True)
                _set(uid, shopper=shopper, fase="aguardando_login", erro="",
                     session_id=runtime.session_id, viewport=runtime.viewport)
                deadline, logged = time.time() + LOGIN_DEADLINE_S, False
                # O loop desperta no input para bombear frames e drenar a fila, mas
                # nem o cache nem o DOM mudam nessa escala. Ler os dois a cada
                # volta custava, POR SEGUNDO, 20 idas ao cache e 20 consultas de
                # seletor CSS via CDP — dentro do processo do gunicorn, segurando a
                # GIL e derrubando a latência das outras 7 threads. O ML já tinha
                # essa correção ("ler o cache 20x/s só gastava CPU"); a Amazon nunca
                # recebeu. Meio segundo de latência num clique de cancelar ninguém
                # percebe.
                state = {}
                proxima_leitura = proxima_checagem = 0.0
                while time.time() < deadline:
                    agora = time.time()
                    if agora >= proxima_leitura:
                        state = cache.get(_key(uid, shopper)) or {}
                        proxima_leitura = agora + 0.5
                    if state.get("cancelar"):
                        _set(uid, shopper=shopper, fase="idle", erro="")
                        break
                    current_page = active_page.current()
                    if current_page is None:
                        raise RuntimeError(f"A janela da {_store_name(shopper)} foi fechada.")
                    houve_input = False
                    for _ in range(MAX_EVENTOS_POR_POST * 4):
                        try:
                            evento = runtime.input_queue.get_nowait()
                        except queue.Empty:
                            break
                        despachar_input(current_page, evento)
                        houve_input = True
                    transport.capture(runtime, current_page, active=houve_input)

                    validacao_manual = bool(state.get("salvar_agora"))
                    if validacao_manual:
                        # O botão de salvar também valida a sessão na página que o
                        # sincronizador usará, sem depender da URL da landing.
                        _set(uid, shopper=shopper, fase="validando", salvar_agora=False)
                        state["salvar_agora"] = False
                        # Depois de navegar, confere já: é o momento em que o
                        # resultado muda.
                        proxima_checagem = 0.0
                        try:
                            current_page.goto(
                                (SHOPEE_PROFILE_URL if _is_shopee(shopper)
                                 else SHOP_URL if shopper else REPORT_URL),
                                wait_until="domcontentloaded",
                                              timeout=GOTO_TIMEOUT_MS)
                            transport.capture(runtime, current_page, active=True)
                        except Exception:
                            # Um "já entrei" prematuro (ou rede lenta) não pode
                            # encerrar a sessão: o usuário continua no login.
                            logger.warning(
                                "Validação do login %s não navegou (user %s).",
                                _store_name(shopper), uid, exc_info=True)
                            _set(uid, shopper=shopper, fase="aguardando_login",
                                 aviso="Ainda não foi possível confirmar o login. "
                                       "Conclua a etapa aberta e tente de novo.")
                    # `pode_inspecionar` adia a checagem enquanto o usuário digita: o
                    # `_logado` é uma consulta de seletor por CDP na mesma thread que
                    # precisa aplicar a tecla e capturar a tela. O "já entrei" não
                    # espera — a fase já foi para "validando" e adiar deixaria a tela
                    # presa nela sem veredito.
                    if agora >= proxima_checagem and (
                        validacao_manual or pode_inspecionar(runtime)
                    ):
                        proxima_checagem = agora + 1.0
                        if _logado(current_page, shopper=shopper):
                            logged = True
                            logger.info(
                                "ml_login_metric transport=%s user=%s validation=conectado",
                                ("shopee_loja" if _is_shopee(shopper) else
                                 "amazon_loja" if shopper else "amazon_associados"), uid,
                            )
                            break
                    aguardar_atividade(current_page, runtime)
                if logged:
                    _set(uid, shopper=shopper, fase="salvando")
                    # Só a leitura fica aqui. Gravar depois do bloco mantém o mesmo
                    # formato dos outros workers e garante que a fase só vire
                    # 'conectado' quando a sessão estiver de fato no disco.
                    estado_capturado = context.storage_state()
                elif (cache.get(_key(uid, shopper)) or {}).get("fase") != "idle":
                    _set(uid, shopper=shopper, fase="erro",
                         erro=f"Tempo esgotado esperando o login da {_store_name(shopper)}.")
            finally:
                # Em finally: solto no fim do bloco, o close nao era alcancado
                # em nenhuma excecao e o Chromium ficava orfao.
                try:
                    browser.close()
                except Exception:
                    logger.warning("Chromium do login %s não fechou limpo (user %s).",
                                   _store_name(shopper), uid, exc_info=True)

        if estado_capturado is not None:
            save_report_state(user, _session_provider(shopper), estado_capturado)
            _set(uid, shopper=shopper, fase="conectado", erro="", aviso="",
                 salvar_agora=False)
    except Exception as exc:
        codigo = novo_codigo()
        logger.exception("Conexão %s falhou (user=%s codigo=%s)",
                         _store_name(shopper), uid, codigo)
        _set(uid, shopper=shopper, fase="erro", codigo_erro=codigo,
             erro=mensagem_de_erro(exc, codigo, servico=f"A {_store_name(shopper)}"))
    finally:
        transport.finish(uid, runtime)
        with _lock:
            _threads.pop((uid, shopper), None)


def criar_sessao(user, client: dict | None = None, *, shopper=False):
    from apps.accounts.feature_flags import enabled_for_user
    flag = ("SHOPEE_BROWSER_LOGIN_ENABLED" if _is_shopee(shopper)
            else "AMAZON_BROWSER_LOGIN_ENABLED")
    if not enabled_for_user(flag, user):
        # Persistido, e não só retornado: o poll do front relê o cache e um estado
        # efêmero seria imediatamente apagado pela fase antiga — a tela repintava
        # "Conectado" sobre um login que nunca abriu (ver a mesma correção em
        # ml_conexao.criar_sessao). Esta era a única das três telas de login sem
        # kill-switch: um problema no gateway da Amazon não tinha como ser contido
        # sem deploy.
        return {
            **_set(user.id, shopper=shopper, fase="indisponivel", cancelar=False,
                   salvar_agora=False,
                   erro=f"Login da {_store_name(shopper)} por navegador está desativado para esta organização."),
            "auth_valido": False,
        }
    with _lock:
        thread_key = (user.id, shopper)
        running = _threads.get(thread_key)
        if running and running.is_alive():
            return status(user.id, shopper=shopper)
        runtime = _live_transport(shopper).create(user.id, client)
        _set(user.id, shopper=shopper, fase="iniciando", erro="", aviso="",
             cancelar=False,
             salvar_agora=False, session_id=runtime.session_id,
             viewport=runtime.viewport)
        thread = threading.Thread(target=_worker, args=(user, shopper), daemon=True)
        _threads[thread_key] = thread
        thread.start()
    return status(user.id, shopper=shopper)


def frames(user_id, session_id=None, *, shopper=False):
    yield from _live_transport(shopper).frames(user_id, session_id)


def enfileirar_input(user_id, session_id, eventos, *, shopper=False):
    return _live_transport(shopper).enqueue(user_id, session_id, eventos)


def salvar_agora(user_id, *, shopper=False):
    _set(user_id, shopper=shopper, salvar_agora=True)


def cancelar(user_id, *, shopper=False):
    _set(user_id, shopper=shopper, cancelar=True, fase="idle", erro="")
