import os
import random
import inspect
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

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
def iniciar_browser(precisa_logar=False, auth_path=None, headless=False,
                    permitir_login=False, **context_kwargs):
    """`permitir_login`: True só quando há HUMANO (view auth_stream) p/ escanear/logar.
    Nos workers de raspagem (headless) fica False: sessão morta -> SessaoExpirada."""
    context_kwargs.setdefault("user_agent", ua_aleatorio())
    if auth_path is None:
        caller_dir = os.path.dirname(os.path.abspath(inspect.stack()[1].filename))
        auth_path = os.path.join(caller_dir, "auth.json")

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
            if not permitir_login:
                # Worker sem humano: apaga a sessão morta (monitor passa a reportar
                # 'desconectado' e dispara e-mail) e aborta a raspagem desta fonte.
                if os.path.exists(auth_path):
                    try:
                        os.remove(auth_path)
                    except OSError:
                        pass
                raise SessaoExpirada("Sessão ML expirada — reconecte pela tela Conexões.")
            precisa_logar = True
        else:
            print("Sessão validada")
    if precisa_logar:    
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(storage_state=auth_path, **context_kwargs) if os.path.exists(auth_path) else browser.new_context(**context_kwargs)
                page = context.new_page()
                page.goto("https://www.mercadolivre.com/jms/mlb/lgz/msl/login/")
                page.wait_for_load_state("networkidle")
                if os.path.exists(auth_path):
                    os.remove(auth_path)
                print("LOGIN_REQUIRED")
                page.wait_for_url("https://www.mercadolivre.com.br/", timeout=180000)
                context.storage_state(path=auth_path)
                browser.close()
                print("Login salvo com sucesso!")

            except Exception as e:
                raise BrowserError(f"Erro durante o processo de login: {e}")

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
            try:
                context.storage_state(path=auth_path)
            except Exception:
                pass
            context.close()
            browser.close()



