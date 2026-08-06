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

# O `go` é o parâmetro de retorno do SSO do ML: depois do login (inclusive depois de
# SMS/app) ele devolve o navegador para uma página conhecida, em vez de parar num
# destino que a sonda de sessão não reconhece. Entrar na rota de login sem ele era um
# caminho que nenhum usuário real percorre.
LOGIN_URL = (
    "https://www.mercadolivre.com/jms/mlb/lgz/msl/login/"
    "?go=https%3A%2F%2Fwww.mercadolivre.com.br%2F"
)
RECARGAS_APOS_ERRO = 1           # o gateway do ML às vezes aceita na 2ª tentativa

LOGIN_DEADLINE_S = 600           # tempo máx. esperando o usuário logar
LOOP_MS = 50                     # granularidade do worker (bombeia CDP + drena input)
GOTO_TIMEOUT_MS = 60000          # em prod (IP de datacenter) o gateway de login demora
GOTO_TENTATIVAS = 2              # timeout na 1ª tenta de novo antes de desistir
SNAPSHOT_INTERVAL_S = 2.5        # de quanto em quanto tempo olhamos os cookies

MAX_EVENTOS_POR_POST = 60        # teto de eventos por request (anti-abuso da fila)

# Estado em memória DESTE worker (o cache guarda fase/erro visível entre threads).
# Frames e fila de input vivem no LiveTransport, que os três fluxos compartilham.
_threads: dict[int, threading.Thread] = {}
_lock = threading.Lock()
_transport = LiveTransport("mercado_livre")


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


def _estado_publico(estado: dict) -> dict:
    """Remove comandos internos antes de devolver o estado ao navegador."""
    publico = dict(estado)
    for comando in ("retentar_qr", "cancelar", "salvar_agora", "validar_agora"):
        publico.pop(comando, None)
    return publico


def status(user_id: int) -> dict:
    """Estado atual da conexão pro polling do front.

    fase: 'idle' | 'iniciando' | 'aguardando_login' | 'configurar_qr' |
          'salvando' | 'conectado' | 'erro'
    """
    estado = dict(cache.get(_cache_key(user_id)) or {"fase": "idle"})
    # Durante o login em curso o veredito da sessão salva não muda nada na tela
    # (a fase já manda), e em cache-miss a sonda custa até 8s de rede. Com o
    # front pedindo status a cada 3s, isso empilhava requisições nas 8 threads do
    # gunicorn e atrasava a pintura da imagem.
    if estado.get("fase") in {
        "iniciando", "aguardando_login", "configurar_qr", "validando",
        "validando_linkbuilder", "aguardando_linkbuilder", "salvando",
    }:
        estado["auth_valido"] = False
        estado["motivo_desconexao"] = ""
        estado["alerta_conexao"] = ""
        estado["linkbuilder"] = _LINKBUILDER_VAZIO
        estado.update(_transport.status(user_id))
        return _estado_publico(estado)
    # 'conectado' de verdade vem da fonte única (conexoes.py) — a mesma que o
    # dashboard e a Saúde leem. A `fase` acima é só o progresso do login em curso.
    try:
        from django.contrib.auth import get_user_model
        from apps.scrapers.conexoes import estado_ml, estado_ml_linkbuilder

        user = get_user_model().objects.filter(id=user_id).first()
        est = estado_ml(user) if user else None
        estado["auth_valido"] = bool(est and est.conectado)
        estado["motivo_desconexao"] = est.motivo if est and not est.conectado else ""
        # Conectado, mas a última sonda não passou. A tela precisa dizer isso sem
        # alarmar: o anti-bot do ML bloqueia requisições legítimas vindas do IP da
        # Fly, e a conexão volta ao normal sozinha na maioria das vezes.
        estado["alerta_conexao"] = est.alerta if est and est.conectado else ""
        # Segunda linha da tela: o portal de afiliados tem SSO próprio e pode
        # recusar o cookie que o site aceita. Só medimos quando o site está OK —
        # sem sessão nenhuma, o aviso relevante é o de cima, e duplicá-lo faria a
        # tela dar dois diagnósticos para o mesmo problema.
        estado["linkbuilder"] = (
            _bloco_linkbuilder(estado_ml_linkbuilder(user))
            if user and estado["auth_valido"] else _LINKBUILDER_VAZIO
        )
    except Exception:
        from apps.accounts.ml_sessions import has_storage_state
        user = get_user_model().objects.filter(id=user_id).first()
        estado["auth_valido"] = bool(user and has_storage_state(user))
        estado["motivo_desconexao"] = ""
        estado["alerta_conexao"] = ""
        estado["linkbuilder"] = _LINKBUILDER_VAZIO
    estado.update(_transport.status(user_id))
    return _estado_publico(estado)


# Ausência de veredito, não veredito negativo: a tela renderiza "—" em vez de
# afirmar que o Link Builder está de pé ou caído.
_LINKBUILDER_VAZIO = {
    "medido": False,
    "estado": "unknown",
    "ok": False,
    "motivo": "",
    "alerta": "",
    "verificado_em": None,
}


def _bloco_linkbuilder(est) -> dict:
    return {
        "medido": True,
        "estado": est.detalhe or ("ready" if est.conectado else "unknown"),
        "ok": bool(est.conectado),
        "motivo": "" if est.conectado else est.motivo,
        "alerta": est.alerta or "",
        "verificado_em": (
            est.verificado_em.isoformat()
            if hasattr(est.verificado_em, "isoformat") else est.verificado_em
        ),
    }


def _marcar_configuracao_qr(user_id: int, contexto: str) -> dict:
    """Pausa o login e publica o onboarding sem incrementar a cada leitura do DOM."""
    atual = dict(cache.get(_cache_key(user_id)) or {})
    desafio_anterior = atual.get("desafio")
    desafio_anterior = desafio_anterior if isinstance(desafio_anterior, dict) else {}
    if (
        atual.get("fase") == "configurar_qr"
        and desafio_anterior.get("contexto") == contexto
    ):
        return atual

    try:
        tentativas_anteriores = max(0, int(desafio_anterior.get("tentativas") or 0))
    except (TypeError, ValueError):
        tentativas_anteriores = 0
    tentativas = (
        tentativas_anteriores + 1
        if desafio_anterior.get("contexto") == contexto else 1
    )
    logger.info(
        "ml_login_metric transport=mercado_livre user=%s "
        "challenge=camera_not_found context=%s attempt=%s",
        user_id, contexto, tentativas,
    )
    return _set_estado(
        user_id,
        fase="configurar_qr",
        desafio={
            "tipo": "camera_indisponivel",
            "contexto": contexto,
            "tentativas": tentativas,
        },
        retentar_qr=False,
        aviso="",
        erro="",
    )


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


def _pagina_de_erro_do_ml(page) -> bool:
    """True quando o ML respondeu com a própria página de erro genérica.

    É o "Ops! Ocorreu um erro. Tente novamente mais tarde." que o gateway devolve
    quando recusa a sessão vinda do servidor. Sem detectar isso, o worker seguia em
    `aguardando_login` até o deadline de 10 minutos: o usuário ficava olhando uma tela
    que nunca ia sair do lugar, e nem ele nem o log tinham como saber o motivo.

    Usa `textContent` e não `innerText` de propósito: não força layout na página que o
    live view está capturando ao mesmo tempo.
    """
    try:
        texto = page.evaluate(
            "() => ((document.title || '') + ' ' +"
            " ((document.body && document.body.textContent) || ''))"
            ".slice(0, 4000).toLowerCase()"
        )
    except Exception:
        return False
    if not isinstance(texto, str):
        return False
    if not any(m in texto for m in ("ocorreu um erro", "ocurrió un error")):
        return False
    if not any(m in texto for m in ("tente novamente mais tarde", "más tarde")):
        return False
    # A página de erro não tem formulário. Um campo de senha/e-mail presente significa
    # que estamos na tela de login com alguma mensagem de validação — que é normal e
    # não deve virar um alerta de bloqueio.
    try:
        if page.locator("input[type='password'], input[type='email']").count():
            return False
    except Exception:
        return False
    return True


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


def _gravar_sessao(user_id: int, storage_state: dict, prontidao: str = "unknown",
                   motivo_prontidao: str = "") -> None:
    """Grava a sessão capturada e derruba o cache da sonda. Roda no tenant, fora do loop."""
    from django.contrib.auth import get_user_model
    from apps.accounts.ml_sessions import (
        registrar_prontidao_linkbuilder_para_usuario,
        save_storage_state,
    )
    from apps.scrapers.conexoes import invalidar_ml

    user = get_user_model().objects.get(pk=user_id)
    save_storage_state(user, storage_state)
    registrar_prontidao_linkbuilder_para_usuario(
        user, prontidao, motivo_prontidao,
    )
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


def _persistir_sessao(user_id: int, storage_state: dict, tentativas: int = 3,
                      prontidao: str = "unknown",
                      motivo_prontidao: str = "") -> None:
    """Grava a sessão, tolerante a socket morto depois de minutos de browser ocioso."""
    from django.db import InterfaceError, OperationalError, close_old_connections

    for tentativa in range(1, tentativas + 1):
        try:
            executar_no_tenant(
                _gravar_sessao, user_id, storage_state,
                prontidao, motivo_prontidao,
            )
            return
        except (OperationalError, InterfaceError) as exc:
            if tentativa == tentativas:
                raise
            logger.warning("Gravação da sessão ML falhou (%s/%s): %s",
                           tentativa, tentativas, exc)
            close_old_connections()
            time.sleep(0.5 * tentativa)


def _validar_linkbuilder_ao_vivo(user_id: int, active_page, runtime, context,
                                 deadline: float) -> tuple[str | None, str]:
    """Abre o portal no mesmo navegador e mantém a live view até o veredito.

    ``None`` significa cancelamento explícito. Os demais estados são persistidos
    somente depois que o Playwright fecha.
    """
    from apps.scrapers.scraper_mercadolivre.link import (
        _LB_TIMEOUT_MS,
        _LB_URL,
        _linkbuilder_pronto,
        _pagina_de_login,
        _pagina_intersticial,
    )
    from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

    current_page = active_page.current()
    if current_page is None:
        return "temporarily_unavailable", "a janela do Link Builder foi fechada"
    _set_estado(
        user_id, fase="validando_linkbuilder", aviso=(
            "Login confirmado. Conferindo agora o Link Builder real…"
        ),
    )
    try:
        current_page.goto(
            _LB_URL, wait_until="domcontentloaded", timeout=_LB_TIMEOUT_MS,
        )
        try:
            current_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
    except Exception:
        logger.warning(
            "Link Builder não concluiu a navegação durante o login (user %s).",
            user_id, exc_info=True,
        )

    ultimo_estado = "temporarily_unavailable"
    ultimo_motivo = "o Link Builder não ficou disponível"
    last_beat = 0.0
    proxima_checagem_qr = 0.0
    qr_deadline_estendido = False
    while time.time() < deadline:
        estado = cache.get(_cache_key(user_id)) or {}
        if estado.get("cancelar"):
            if estado.get("fase") == "configurar_qr":
                logger.info(
                    "ml_login_metric transport=mercado_livre user=%s "
                    "challenge=camera_not_found action=cancel context=linkbuilder",
                    user_id,
                )
            _set_estado(user_id, fase="idle", desafio={}, aviso="", erro="")
            return None, "cancelado pelo usuário"

        current_page = active_page.current()
        if current_page is None:
            return "temporarily_unavailable", "a janela do Link Builder foi fechada"

        if estado.get("fase") == "configurar_qr":
            if estado.get("retentar_qr"):
                logger.info(
                    "ml_login_metric transport=mercado_livre user=%s "
                    "challenge=camera_not_found action=retry context=linkbuilder",
                    user_id,
                )
                estado = _set_estado(
                    user_id, fase="validando_linkbuilder", retentar_qr=False,
                    aviso="Reabrindo o Link Builder…", erro="",
                )
                try:
                    current_page.goto(
                        _LB_URL, wait_until="domcontentloaded", timeout=_LB_TIMEOUT_MS,
                    )
                    _transport.capture(runtime, current_page, active=True)
                except Exception:
                    logger.info(
                        "ml_login_metric transport=mercado_livre user=%s "
                        "challenge=camera_not_found action=retry_failed "
                        "context=linkbuilder",
                        user_id,
                    )
                    logger.warning(
                        "Link Builder não reabriu depois da configuração do QR "
                        "(user %s).", user_id, exc_info=True,
                    )
                    _set_estado(
                        user_id, fase="aguardando_linkbuilder", aviso=(
                            "O Link Builder não respondeu agora. A janela continua "
                            "aberta; tente verificar novamente."
                        ),
                    )
            current_page.wait_for_timeout(LOOP_MS)
            continue

        houve_input = False
        for _ in range(MAX_EVENTOS_POR_POST * 4):
            try:
                evento = runtime.input_queue.get_nowait()
            except queue.Empty:
                break
            despachar_input_v2(current_page, evento)
            houve_input = True
        _transport.capture(runtime, current_page, active=houve_input)

        agora = time.time()
        if agora >= proxima_checagem_qr:
            proxima_checagem_qr = agora + 1.0
            if pagina_exige_configuracao_qr(current_page):
                if not qr_deadline_estendido:
                    deadline = agora + LOGIN_DEADLINE_S
                    qr_deadline_estendido = True
                _marcar_configuracao_qr(user_id, "linkbuilder")
                current_page.wait_for_timeout(LOOP_MS)
                continue

        if _linkbuilder_pronto(current_page):
            return "ready", "campo de URL e botão Gerar confirmados"
        if _pagina_de_login(current_page):
            ultimo_estado = "login_required"
            ultimo_motivo = "o portal de afiliados pediu login"
            _set_estado(
                user_id, fase="aguardando_linkbuilder", aviso=(
                    "O Link Builder pediu uma etapa adicional. Conclua o login "
                    "ou a verificação nesta janela."
                ),
            )
        elif _pagina_intersticial(current_page):
            ultimo_estado = "temporarily_unavailable"
            ultimo_motivo = "verificação de segurança interposta"
            _set_estado(
                user_id, fase="aguardando_linkbuilder", aviso=(
                    "O Mercado Livre pediu uma verificação de segurança. "
                    "Conclua a etapa nesta janela para validar o Link Builder."
                ),
            )
        else:
            ultimo_estado = "temporarily_unavailable"
            ultimo_motivo = "os controles do Link Builder não apareceram"
            _set_estado(user_id, fase="validando_linkbuilder")

        if time.time() - last_beat > 8:
            _set_estado(user_id)
            last_beat = time.time()
        current_page.wait_for_timeout(LOOP_MS)

    return ultimo_estado, ultimo_motivo


@organization_job_sem_transacao
def _worker(user_id: int):
    """Sobe o Chromium, publica capturas nítidas e valida a sessão de verdade."""
    from playwright.sync_api import sync_playwright
    from apps.scrapers.contexto_login import (
        LAUNCH_ARGS, habilitar_foco, opcoes_de_contexto,
    )
    from apps.scrapers.conexoes import sondar_sessao_ml
    from apps.scrapers.ml_login_challenge import pagina_exige_configuracao_qr

    runtime = _transport.get(user_id) or _transport.create(user_id)

    estado_capturado = None
    validator = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ml-auth-probe")
    pending_validation = None
    pending_state = None
    last_fingerprint = ""
    manual_validation = False
    # Zero garante uma primeira leitura de cookies logo na entrada do loop.
    ultima_leitura_storage = 0.0
    recargas = 0
    erro_ml_avisado = False
    last_error_check = 0.0
    prontidao_linkbuilder = "unknown"
    motivo_linkbuilder = ""
    qr_deadline_estendido = False
    try:
        _set_estado(
            user_id, fase="iniciando", desafio={}, retentar_qr=False,
            aviso="", erro="",
        )
        with sync_playwright() as p:
            # Flags do scraper (auxiliar.iniciar_browser) + dev-shm p/ não crashar o
            # Chromium em container com /dev/shm pequeno (Fly) + locale, ver
            # contexto_login.
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                context = browser.new_context(**opcoes_de_contexto(
                    browser, runtime.viewport,
                    permissions=["clipboard-read", "clipboard-write"],
                ))
                page = context.pages[0] if context.pages else context.new_page()
                habilitar_foco(context, page)
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
                        if estado.get("fase") == "configurar_qr":
                            logger.info(
                                "ml_login_metric transport=mercado_livre user=%s "
                                "challenge=camera_not_found action=cancel context=%s",
                                user_id,
                                (estado.get("desafio") or {}).get("contexto", "login"),
                            )
                        _set_estado(
                            user_id, fase="idle", desafio={}, retentar_qr=False,
                            aviso="", erro="",
                        )
                        break

                    current_page = active_page.current()
                    if current_page is None:
                        raise RuntimeError("A janela de login do Mercado Livre foi fechada.")

                    # Enquanto o cliente ativa o QR no celular, o Chromium continua
                    # vivo, mas não desperdiça screenshot/entrada nem mantém o SSE.
                    # O botão do onboarding só levanta esta flag; a navegação ocorre
                    # aqui, na mesma thread que é dona do Playwright.
                    if estado.get("fase") == "configurar_qr":
                        if estado.get("retentar_qr"):
                            logger.info(
                                "ml_login_metric transport=mercado_livre user=%s "
                                "challenge=camera_not_found action=retry context=login",
                                user_id,
                            )
                            estado = _set_estado(
                                user_id, fase="aguardando_login", retentar_qr=False,
                                aviso="Reabrindo o login do Mercado Livre…", erro="",
                            )
                            if pending_validation is not None:
                                pending_validation.cancel()
                                pending_validation = None
                            pending_state = None
                            manual_validation = False
                            last_fingerprint = ""
                            ultima_leitura_storage = 0.0
                            recargas = 0
                            erro_ml_avisado = False
                            try:
                                _ir_para_login(current_page)
                            except Exception:
                                logger.info(
                                    "ml_login_metric transport=mercado_livre user=%s "
                                    "challenge=camera_not_found action=retry_failed "
                                    "context=login",
                                    user_id,
                                )
                                raise
                            _transport.capture(runtime, current_page, active=True)
                            last_error_check = 0.0
                        current_page.wait_for_timeout(LOOP_MS)
                        continue

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

                    # O gateway do ML pode responder com a própria página de erro em vez
                    # da tela de login. Antes disso passar batido, o worker seguia
                    # esperando um login que já havia sido recusado até o deadline de 10
                    # minutos, e o log não registrava nada.
                    if agora - last_error_check > SNAPSHOT_INTERVAL_S:
                        last_error_check = agora
                        if pagina_exige_configuracao_qr(current_page):
                            if not qr_deadline_estendido:
                                deadline = agora + LOGIN_DEADLINE_S
                                qr_deadline_estendido = True
                            if pending_validation is not None:
                                pending_validation.cancel()
                                pending_validation = None
                            pending_state = None
                            manual_validation = False
                            estado = _marcar_configuracao_qr(user_id, "login")
                            current_page.wait_for_timeout(LOOP_MS)
                            continue
                        if _pagina_de_erro_do_ml(current_page):
                            logger.info(
                                "ml_login_metric transport=mercado_livre user=%s "
                                "gateway=pagina_de_erro recargas=%s",
                                user_id, recargas,
                            )
                            if recargas < RECARGAS_APOS_ERRO:
                                recargas += 1
                                _set_estado(user_id, fase="aguardando_login", aviso=(
                                    "O Mercado Livre recusou a primeira tentativa. "
                                    "Abrindo o login novamente…"
                                ))
                                _ir_para_login(current_page)
                                _transport.capture(runtime, current_page, active=True)
                            elif not erro_ml_avisado:
                                erro_ml_avisado = True
                                _set_estado(user_id, fase="aguardando_login", aviso=(
                                    "O Mercado Livre está recusando o login a partir do "
                                    "servidor (a mensagem \"Ocorreu um erro\" na janela "
                                    "vem dele, não do Spreading). Aguarde alguns "
                                    "minutos e tente novamente."
                                ))

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

                    # storage_state() é um round-trip CDP + serialização de todos os
                    # cookies. No loop de 50ms isso rodava 20x/s, atrasando o drain
                    # da fila de input e as capturas — a digitação engasgava. Cookie
                    # de login não muda em milissegundos.
                    #
                    # Com uma sonda EM VOO o snapshot é puro desperdício: o `submit`
                    # abaixo seria descartado de qualquer forma, e a mudança acumulada
                    # aparece no snapshot seguinte. Uma verificação manual ("já entrei")
                    # não espera o intervalo.
                    agora = time.monotonic()
                    if pending_validation is None and (
                        manual_validation
                        or agora - ultima_leitura_storage >= SNAPSHOT_INTERVAL_S
                    ):
                        ultima_leitura_storage = agora
                        snapshot = context.storage_state()
                        fingerprint = _storage_fingerprint(snapshot)
                        changed = fingerprint != last_fingerprint
                        if changed:
                            last_fingerprint = fingerprint
                        if changed or manual_validation:
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
                    prontidao_linkbuilder, motivo_linkbuilder = (
                        _validar_linkbuilder_ao_vivo(
                            user_id, active_page, runtime, context, deadline,
                        )
                    )
                    if prontidao_linkbuilder is None:
                        logado = False
                        estado_capturado = None
                    else:
                        _set_estado(user_id, fase="salvando")
                        # Captura de novo: o SSO do portal pode ter renovado ou
                        # acrescentado cookies depois da validação do site.
                        estado_capturado = context.storage_state()
                else:
                    estado_final = cache.get(_cache_key(user_id)) or {}
                    if estado_final.get("fase") != "idle":
                        esperando_qr = estado_final.get("fase") == "configurar_qr"
                        _set_estado(
                            user_id, fase="erro", retentar_qr=False,
                            erro=(
                                "O tempo para configurar o QR terminou. "
                                "Inicie uma nova conexão e tente novamente."
                                if esperando_qr else
                                "Tempo esgotado esperando o login. Tente de novo."
                            ),
                        )
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
            _persistir_sessao(
                user_id, estado_capturado,
                prontidao=prontidao_linkbuilder,
                motivo_prontidao=motivo_linkbuilder,
            )
            desafio_concluido = (cache.get(_cache_key(user_id)) or {}).get("desafio")
            if isinstance(desafio_concluido, dict) and desafio_concluido.get("tipo"):
                logger.info(
                    "ml_login_metric transport=mercado_livre user=%s "
                    "challenge=camera_not_found action=success context=%s attempts=%s",
                    user_id,
                    desafio_concluido.get("contexto", "login"),
                    desafio_concluido.get("tentativas", 1),
                )
            aviso_final = ""
            if prontidao_linkbuilder == "login_required":
                aviso_final = (
                    "A conta principal foi salva, mas o Link Builder ainda pediu "
                    "login. Reconecte para concluir essa etapa."
                )
            elif prontidao_linkbuilder == "temporarily_unavailable":
                aviso_final = (
                    "A conta foi salva; o Link Builder ficou temporariamente "
                    "indisponível e será testado novamente no próximo uso."
                )
            _set_estado(
                user_id, fase="conectado", salvar_agora=False,
                validar_agora=False, retentar_qr=False, desafio={},
                aviso=aviso_final, erro="",
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
            salvar_agora=False, validar_agora=False, retentar_qr=False,
            desafio={}, session_id=runtime.session_id, viewport=runtime.viewport,
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


def retentar_apos_configurar_qr(user_id: int) -> tuple[bool, dict]:
    """Sinaliza a retomada; somente a thread dona do Playwright navega a página."""
    with _lock:
        thread = _threads.get(user_id)
        ativa = bool(
            thread and thread.is_alive()
            and _transport.get(user_id) is not None
        )
    if not ativa:
        return False, {
            "ok": False,
            "erro": "A sessão de login terminou. Inicie uma nova conexão.",
        }

    atual = cache.get(_cache_key(user_id)) or {}
    if atual.get("fase") != "configurar_qr":
        return False, {
            "ok": False,
            "erro": "O login não está aguardando a configuração do QR.",
        }

    _set_estado(user_id, retentar_qr=True, aviso="", erro="")
    resposta = status(user_id)
    resposta["ok"] = True
    return True, resposta


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

    return _set_estado(
        user_id, fase="conectado", desafio={}, retentar_qr=False,
        erro="", salvar_agora=False, cancelar=False,
    )


def salvar_agora(user_id: int):
    """Usuário pediu uma validação; nunca força a persistência de cookies inválidos."""
    _set_estado(user_id, validar_agora=True, salvar_agora=False, aviso="")


def cancelar(user_id: int):
    atual = cache.get(_cache_key(user_id)) or {}
    if atual.get("fase") == "configurar_qr":
        logger.info(
            "ml_login_metric transport=mercado_livre user=%s "
            "challenge=camera_not_found action=cancel context=%s",
            user_id, (atual.get("desafio") or {}).get("contexto", "login"),
        )
    _set_estado(
        user_id, cancelar=True, fase="idle", desafio={}, retentar_qr=False,
        aviso="", erro="",
    )


def esquecer(user_id: int) -> None:
    """Zera o estado do login deste usuário.

    Sem isto, o "Desconectar" apagava a sessão no banco mas o poll seguinte ainda
    lia a fase antiga do cache — e a tela voltava a se pintar de verde por até 14
    minutos sobre uma sessão que já não existia.
    """
    cache.delete(_cache_key(user_id))
