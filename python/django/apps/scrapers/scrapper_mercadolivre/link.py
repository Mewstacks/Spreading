import sys
import os
caminho_atual = os.path.dirname(os.path.abspath(__file__))
caminho_raiz = os.path.dirname(caminho_atual)
sys.path.append(caminho_raiz)
from django.scrapers.auxiliar import iniciar_browser, BrowserError


class LoginError(Exception):
    """Exceção personalizada para erros de login."""
    pass

class AuthError(Exception):
    """Exceção personalizada para erros de autenticação, quando o ML bloqueia essa merda."""
    pass





def afiliate_link_builder(link_base):    
    with iniciar_browser(
        auth_path=os.path.join(caminho_atual, "auth.json"),
        headless=True, 
        permissions=['clipboard-read', 'clipboard-write'], 
    ) as (page, context):
        try:
            page.goto("https://www.mercadolivre.com.br/afiliados/linkbuilder#hub")
        except:
            raise AuthError("Não foi possível acessar o Link Builder. Verifique sua conexão e se a sessão está ativa, ou tente com o headless=False seu macaco.")
        
        login_field = page.get_by_test_id("user_id")
        if login_field.is_visible(timeout=10000):
            raise LoginError("Faça login e rode a função novamente para gerar o link de afiliado, seu bosta.")
        print("ta logado nessa porra")
        
        try:
            page.get_by_role("textbox", name="Insira 1 ou mais URLs").fill(link_base)
            page.get_by_role("button", name="Gerar").click()

            page.get_by_role("button", name="Copiar").click()
            link_final = page.evaluate("navigator.clipboard.readText()")
            
            if not link_final or len(link_final) < 5:
                raise ValueError("Link de afiliado não gerado corretamente.")
                
            print(f"Sucesso! O link gerado foi: {link_final}")
            return link_final

        except Exception as e:
            print(f"Erro ao gerar o link de afiliado: {e}")
            return None
        

# Testando a função
meu_link = afiliate_link_builder("https://www.mercadolivre.com.br/rob-aspirador-de-po-inteligente-wi-fi-kuanttum-mop-lava-e-seca-agua-quente-varre-aspira-3-em-1-robot-10000pa-autolimpante-x20-passa-pano-mapeamento-cor-preto-220v-com-base-estaco/p/MLB67993128?pdp_filters=item_id:MLB4609148189#is_advertising=true&searchVariation=MLB67993128&backend_model=search-backend&be_origin=backend&position=4&search_layout=grid&type=pad&tracking_id=4d631370-9e18-4e09-89bb-690d4b5c3fe8&ad_domain=VQCATCORE_LST&ad_position=4&ad_click_id=NTJjMTdiYmUtYzJiZC00MGUyLWE3MWUtZTUyNWNkNGI4NWQz")


def cupons_extractor():
    pass