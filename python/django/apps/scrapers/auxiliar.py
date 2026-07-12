import os
import random
import inspect
import logging
from contextlib import contextmanager
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


@contextmanager
def iniciar_browser(precisa_logar=False, auth_path=None, headless=True,
                    validar_sessao=True, **context_kwargs):
    """Servidor é headless: o login do ML é sempre pela web (Conexão Mercado Livre,
    browser remoto com live view). Aqui só validamos/usamos a sessão já salva; se
    ela caiu, sinalizamos SessaoExpirada p/ o monitor reportar 'desconectado'.

    validar_sessao=False: pula a checagem de login (e a possível remoção do auth).
    Use para navegar páginas PÚBLICAS (ex: verificar link de afiliado) onde sessão
    é opcional — evita falso 'Sessão ML expirada' quando o auth do fluxo não existe."""
    context_kwargs.setdefault("user_agent", ua_aleatorio())
    if auth_path is None:
        caller_dir = os.path.dirname(os.path.abspath(inspect.stack()[1].filename))
        auth_path = os.path.join(caller_dir, "auth.json")
    tinha_auth = os.path.exists(auth_path)

    if validar_sessao:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(storage_state=auth_path, **context_kwargs) if os.path.exists(auth_path) else browser.new_context(**context_kwargs)
                page = context.new_page()
                page.goto("https://myaccount.mercadolivre.com.br/my_purchases/list#menu-user")
                page.wait_for_load_state("networkidle")
                deslogado = "login" in page.url
                browser.close()
            except Exception as e:
                raise BrowserError(f"Erro ao iniciar o navegador para checar a sessão: {e}")

            if deslogado:
                # Sessão morta: apaga o arquivo (o monitor passa a reportar 'desconectado'
                # e dispara o alerta) e aborta esta fonte. O usuário reconecta pela web.
                if os.path.exists(auth_path):
                    try:
                        os.remove(auth_path)
                    except OSError:
                        pass
                raise SessaoExpirada("Sessão ML expirada — reconecte em Conexão Mercado Livre.")
            else:
                logger.debug("Sessao Mercado Livre validada")
    if precisa_logar:
        # O login é feito pela web (Conexão Mercado Livre, browser remoto com live
        # view) — NUNCA abrindo um browser visível no servidor headless.
        raise BrowserError(
            "LOGIN_REQUIRED: sessão do Mercado Livre expirou. "
            "Reconecte em Conexão Mercado Livre para continuar."
        )

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(
                headless=headless, 
                args=["--disable-blink-features=AutomationControlled"]
            )
            
            if os.path.exists(auth_path):
                context = browser.new_context(storage_state=auth_path, **context_kwargs)
            else:
                context = browser.new_context(**context_kwargs)
    
            page = context.new_page()
        except Exception as e:
            raise BrowserError(f"Falha crítica ao abrir o navegador: {e}")
        
        try:
            yield page, context # yield serve para passar o controle para o bloco de código que usa o contexto, coisa fofa
            
        finally:
            # Persiste cookies renovados SÓ se já havia sessão salva — contexto anônimo
            # (validar_sessao=False sem auth) não pode criar um auth.json fantasma.
            if tinha_auth:
                try:
                    context.storage_state(path=auth_path)
                except Exception:
                    pass
            context.close()
            browser.close()



