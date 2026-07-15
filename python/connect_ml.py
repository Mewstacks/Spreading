"""Gera a sessão do Mercado Livre localmente (navegador VISÍVEL) e salva auth.json.

O servidor (Fly.io) roda headless — não há tela para o login visível do ML. Então
o admin loga uma vez aqui no próprio PC e envia o resultado pelo painel:

    python connect_ml.py

Uma janela do Chromium abre no ML. Faça login (usuário/senha/2FA), volte ao terminal
e pressione ENTER. O script salva `auth.json` e mostra o caminho. Depois cole o
conteúdo do arquivo no painel: Scraper -> "Enviar sessão ML".

Requer o Playwright (já em requirements.txt):  python -m playwright install chromium
"""
import os
from playwright.sync_api import sync_playwright
DESTINO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth.json")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto("https://www.mercadolivre.com.br/")
        print("\n>>> Faça login no ML na janela que abriu.")
        print(">>> Quando terminar (já logado), volte aqui e pressione ENTER.")
        input()
        ctx.storage_state(path=DESTINO)
        browser.close()
    print(f"\nSessão salva em: {DESTINO}")
    print('Cole o conteúdo desse arquivo no painel: Scraper -> "Enviar sessão ML".')


if __name__ == "__main__":
    main()
