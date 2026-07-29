import os
import random
import logging
from contextlib import contextmanager
from django.db import close_old_connections
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

# Pool de User-Agents reais e atuais — rotaciona por sessão p/ reduzir fingerprint
# (anti-bloqueio, ver pesquisa). Compartilhado com is_alive em ofertas.py via UA_POOL.
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
]


def ua_aleatorio() -> str:
    return random.choice(UA_POOL)


def pausa_humana(min_s: float = 1.5, max_s: float = 4.0):
    """Espera aleatória entre requisições — evita ritmo robótico (anti-bloqueio)."""
    import time
    time.sleep(random.uniform(min_s, max_s))


class BrowserError(Exception):
    """Exceção personalizada para erros relacionados ao navegador."""
    pass


class SessaoExpirada(BrowserError):
    """Sessão do ML caiu (logout). Worker headless NÃO faz login interativo:
    sinaliza p/ o chamador parar a fonte ML e alertar (monitor de conexão)."""
    pass


def _iniciar_chromium(playwright, *, headless):
    """Abre o Chromium do Playwright, com fallback seguro para Chrome instalado.

    Em desenvolvimento é comum instalar o pacote Python sem baixar o binário de
    ~centenas de MB do Playwright. Nesse caso, usar o Chrome já instalado mantém os
    workers de cupons e links funcionais. Outros erros continuam sendo propagados.
    """
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return playwright.chromium.launch(headless=headless, args=args)
    except Exception as erro:
        texto = str(erro)
        if "Executable doesn't exist" not in texto and "playwright install" not in texto:
            raise
        candidatos = [
            os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", ""),
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
        ]
        executavel = next((p for p in candidatos if p and os.path.isfile(p)
                           and os.access(p, os.X_OK)), "")
        if not executavel:
            raise
        logger.info("Chromium do Playwright ausente; usando navegador do sistema.")
        return playwright.chromium.launch(
            headless=headless, executable_path=executavel, args=args)


def _redirecionou_login(url: str) -> bool:
    """True se o ML mandou a navegação p/ a tela de login (sessão caída)."""
    u = (url or "").lower()
    return any(t in u for t in ("/login", "/lgz/", "/registration", "loginhub"))


@contextmanager
def iniciar_browser(precisa_logar=False, auth_path=None, headless=True,
                    session_user=None, storage_state=None, **context_kwargs):
    """Servidor é headless: o login do ML é sempre pela web (Conexão Mercado Livre,
    browser remoto com live view). Aqui só USAMOS a sessão já salva.

    Não existe mais pré-voo de validação (`validar_sessao`). Ele subia um Chromium
    inteiro, navegava até uma página logada e levantava SessaoExpirada se caísse em
    /login — depois fechava tudo e abria um SEGUNDO Chromium para o trabalho real.
    Dois problemas: dobrava o custo do fluxo mais caro do sistema (geração de link,
    ~5s por link) e, no IP de datacenter da Fly, o gateway anti-bot do ML redireciona
    navegações legítimas para challenge/login — então o pré-voo abortava operações
    sobre sessões perfeitamente vivas. Os fluxos que precisam de login já o detectam
    onde ele importa (`link._abrir_link_builder`), e o veredito de "esta sessão
    morreu" é de uma fonte única e acumulativa: conexoes.estado_ml.

    A credencial vem de UM destes, nesta ordem: `storage_state` já resolvido (o
    caso do loop de automação, que usa a organização de sistema e não tem
    usuário — ver scrapers/ml_auth.py), `session_user` (o repositório cifrado
    resolve pela organização dele) ou `auth_path` (só o arquivo legado)."""
    from apps.accounts.ml_session_crypto import MLSessionCryptoError
    from apps.accounts.ml_sessions import load_storage_state, save_storage_state

    context_kwargs.setdefault("user_agent", ua_aleatorio())
    if storage_state is None and session_user is not None:
        try:
            storage_state = load_storage_state(session_user)
        except MLSessionCryptoError as exc:
            raise BrowserError("Sessão ML cifrada inválida; reconecte a conta.") from exc
    elif storage_state is None and auth_path and os.path.isfile(auth_path):
        # Compatibilidade local durante a migração; nunca resolve outro usuário.
        storage_state = auth_path
    tinha_auth = storage_state is not None

    if precisa_logar:
        # O login é feito pela web (Conexão Mercado Livre, browser remoto com live
        # view) — NUNCA abrindo um browser visível no servidor headless.
        raise BrowserError(
            "LOGIN_REQUIRED: sessão do Mercado Livre expirou. "
            "Reconecte em Conexão Mercado Livre para continuar."
        )

    # A gravação no banco NÃO pode acontecer dentro do `with sync_playwright()`: a API
    # sync mantém um event loop vivo num greenlet desta mesma thread, e o @async_unsafe
    # do Django levanta SynchronousOnlyOperation em qualquer query. O `except Exception`
    # que existia aqui engolia esse erro, então NENHUM fluxo renovava cookies — a sessão
    # envelhecia em silêncio até o ML revogá-la. Capturamos o estado aqui dentro (só o
    # Playwright sabe produzi-lo) e gravamos no `finally` de fora, com o loop já morto.
    estado_renovado = None
    try:
        with sync_playwright() as p:
            try:
                browser = _iniciar_chromium(p, headless=headless)

                if storage_state is not None:
                    context = browser.new_context(storage_state=storage_state, **context_kwargs)
                else:
                    context = browser.new_context(**context_kwargs)

                page = context.new_page()
            except Exception as e:
                raise BrowserError(f"Falha crítica ao abrir o navegador: {e}")

            try:
                yield page, context # yield serve para passar o controle para o bloco de código que usa o contexto, coisa fofa

            finally:
                # Persiste cookies renovados SÓ se já havia sessão salva — um contexto
                # anônimo (sem auth) não pode criar uma sessão fantasma.
                if tinha_auth:
                    try:
                        estado_renovado = context.storage_state()
                        if session_user is None and auth_path:
                            # Arquivo (compatibilidade de dev/migração): não é ORM, pode
                            # ser gravado aqui mesmo.
                            context.storage_state(path=auth_path)
                    except Exception:
                        logger.exception("Não foi possível capturar a renovação da sessão ML.")
                context.close()
                browser.close()
    finally:
        # Aqui o loop do Playwright já morreu: o ORM está liberado. Roda mesmo quando o
        # corpo levantou (LoginError, SessaoExpirada) — a sessão renovada até ali continua
        # valendo, e era assim que o `finally` de dentro se comportava.
        if estado_renovado is not None and session_user is not None:
            try:
                close_old_connections()   # minutos de browser matam o socket ocioso
                save_storage_state(session_user, estado_renovado)
            except Exception:
                logger.exception("Falha ao persistir renovação da sessão ML.")


