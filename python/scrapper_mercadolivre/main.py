from playwright.sync_api import sync_playwright
import os

# Nome do arquivo de sessão
auth_file = "auth.json"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    
    # Só tenta carregar se o arquivo realmente existir
    if os.path.exists(auth_file):
        context = browser.new_context(storage_state=auth_file)
    else:
        context = browser.new_context()

    page = context.new_page()
    page.goto("https://www.mercadolivre.com.br/afiliados/hub")

    # Verifica se o campo de login aparece
    # Usamos um timeout curto para não esperar demais se já estiver logado
    login_field = page.get_by_test_id("user_id")
    
    try:
        if login_field.is_visible(timeout=5000):
            print("Sessão não encontrada ou expirada. Faça o login manualmente...")
            page.pause() # O script para aqui para você logar
            
            # Após você logar e fechar o inspetor do pause, ele salva:
            context.storage_state(path=auth_file)
            print("Login realizado e sessão salva!")
        else:
            print("Já estou logado! Prosseguindo...")
    except:
        print("Já estou logado ou a página demorou a responder.")

    # Daqui para baixo seu script continua...
    print("Acessando o Hub de Afiliados...")
    # page.goto(...)
    
    page.pause() # Remova após testar
    browser.close()