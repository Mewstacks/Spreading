from playwright.sync_api import sync_playwright
import os


class LoginError(Exception):
    """Exceção personalizada para erros de login."""
    pass



def run_scrapper(tipo_produto):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        
        # Só tenta carregar se o arquivo realmente existir
        if os.path.exists("auth.json"):
            context = browser.new_context(storage_state="auth.json")
        else:
            context = browser.new_context()

        page = context.new_page()
        page.goto("https://www.mercadolivre.com.br/afiliados/hub")
        login_field = page.get_by_test_id("user_id")
        
        try:
            if login_field.is_visible(timeout=5000):
                print("Sessão não encontrada ou expirada. Faça o login manualmente...")
                page.pause()
                
                context.storage_state(path="auth.json")
            else:
                print("Já estou logado! Prosseguindo...")
        except Exception as e:
            print("Já estou logado ou a página demorou a responder.")

        page.get_by_role("textbox", name="Procurar").click()
        page.get_by_role("textbox", name="Procurar").fill(tipo_produto)
        page.get_by_role("textbox", name="Procurar").press("Enter")    
        page.pause()
        browser.close()




def afiliate_link_builder(link_base):
    link_final = ""
    nav = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"]) #headless=False,
        if os.path.exists("auth.json"):
            context = browser.new_context(storage_state="auth.json", permissions=['clipboard-read', 'clipboard-write'], user_agent=nav)
        else:
            context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'], user_agent=nav)

        page = context.new_page()
        page.goto("https://www.mercadolivre.com.br/afiliados/linkbuilder#hub")
        
        login_field = page.get_by_test_id("user_id")
        if login_field.is_visible(timeout=10000):
            browser.close()
            raise LoginError("Faça login e rode a função novamente para gerar o link de afiliado, seu bosta.")
        else:
            print("ta logado nessa porra")
        try:
            page.get_by_role("textbox", name="Insira 1 ou mais URLs").fill(link_base)
            page.get_by_role("button", name="Gerar").click()

            '''link_completo = page.get_by_role("radio", name="Link completo")
            link_completo.wait_for(state="visible", timeout=10000)
            link_completo.check()'''
        
            page.get_by_role("button", name="Copiar").click()
            link_final = page.evaluate("navigator.clipboard.readText()")
            
            if not link_final or len(link_final) < 5:
                raise ValueError("Link de afiliado não gerado corretamente.")
                
            print(f"Sucesso! O link gerado foi: {link_final}")

        except Exception as e:
            print(f"Erro ao gerar o link de afiliado: {e}")
        
        finally:
            browser.close()
            
        return link_final

# Testando a função
meu_link = afiliate_link_builder("https://www.mercadolivre.com.br/rob-aspirador-de-po-inteligente-wi-fi-kuanttum-mop-lava-e-seca-agua-quente-varre-aspira-3-em-1-robot-10000pa-autolimpante-x20-passa-pano-mapeamento-cor-preto-220v-com-base-estaco/p/MLB67993128?pdp_filters=item_id:MLB4609148189#is_advertising=true&searchVariation=MLB67993128&backend_model=search-backend&be_origin=backend&position=4&search_layout=grid&type=pad&tracking_id=4d631370-9e18-4e09-89bb-690d4b5c3fe8&ad_domain=VQCATCORE_LST&ad_position=4&ad_click_id=NTJjMTdiYmUtYzJiZC00MGUyLWE3MWUtZTUyNWNkNGI4NWQz")