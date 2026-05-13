import os
import inspect
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

class BrowserError(Exception):
    """Exceção personalizada para erros relacionados ao navegador."""
    pass

@contextmanager
def iniciar_browser(precisa_logar=False, auth_path=None, headless=False, **context_kwargs):
    nav = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    context_kwargs.setdefault("user_agent", nav)
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
            if "login" in page.url:
                precisa_logar = True
            else:
                print("Sessão validada")
            browser.close()
        except Exception as e:
            raise BrowserError(f"Erro ao iniciar o navegador para checar a sessão: {e}")
    if precisa_logar:    
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
                context = browser.new_context(storage_state=auth_path, **context_kwargs) if os.path.exists(auth_path) else browser.new_context(**context_kwargs)
                page = context.new_page()
                page.goto("https://myaccount.mercadolivre.com.br/my_purchases/list#menu-user")
                page.wait_for_load_state("networkidle")
                if os.path.exists(auth_path):
                    os.remove(auth_path)
                input("Faça login na sua conta do Mercado Livre e pressione Enter aqui para continuar") 
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



