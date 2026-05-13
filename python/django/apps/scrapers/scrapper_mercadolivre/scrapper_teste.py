import sys
import os
caminho_atual = os.path.dirname(os.path.abspath(__file__))
caminho_raiz = os.path.dirname(caminho_atual)
sys.path.insert(0, caminho_raiz)

from auxiliar import iniciar_browser, BrowserError
import json

def analisar_cupons_e_produtos():
    resultados_finais = []
    auth_path = os.path.join(caminho_atual, "auth.json")

    with iniciar_browser(auth_path=auth_path) as (page, context):
        # URL atualizada para carregar todos os cupons
        print("Acessando a central completa de cupons...")
        page.goto("https://www.mercadolivre.com.br/cupons/filter?all=true&source_page=int_view_all")
        
        page.wait_for_selector("#__NORDIC_RENDERING_CTX__", state="attached")

        # 1. EXTRACAO E LIMPEZA DO JSON
        texto_script = page.locator("#__NORDIC_RENDERING_CTX__").text_content()
        json_puro = texto_script.split("_n.ctx.r=", 1)[1]
        if "};_n.ctx.r" in json_puro:
             json_puro = json_puro.split("};_n.ctx.r")[0] + "}"
        elif json_puro.endswith(";"):
             json_puro = json_puro[:-1]
             
        dados = json.loads(json_puro)
        
        with open("debug_cupom.json", "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=4)
        print("JSON salvo em 'debug_cupom.json'.")
        
        cupons_visuais = dados["appProps"]["pageProps"]["filteredCouponsData"]["coupons"]
        regras_matematicas = dados["appProps"]["pageProps"]["filteredCouponsData"]["tracking"]["view"]["eventData"]["coupons_list"]

        print(f"Total de cupons encontrados no JSON: {len(cupons_visuais)}")

        for index in range(len(cupons_visuais)):
            visual = cupons_visuais[index]
            regra = regras_matematicas[index] 
            titulo = visual.get("title", {}).get("text")
            
            print(f"\n{'-'*50}")
            print(f"A analisar Cupom ({index + 1}/{len(cupons_visuais)}): {titulo}")
            
            produtos_deste_cupom = []
            
            try:
                # PASSO 1: PEGAR O LINK DIRETO DO JSON
                link_produtos = visual.get("action", {}).get("value")
                
                if not link_produtos:
                    print("   Erro: Chave action.value nao encontrada no JSON.")
                    continue
                    
                # Tratamento para links relativos
                if link_produtos.startswith("/"):
                    link_produtos = f"https://www.mercadolivre.com.br{link_produtos}"
                    
                print(f"   Link: {link_produtos}")
                
                # PASSO 2: NAVEGACAO E CALCULO DE DESCONTO
                aba_produtos = context.new_page()
                aba_produtos.goto(link_produtos)
                
                try:
                    aba_produtos.wait_for_selector(".ui-search-layout__item", timeout=10000)
                except Exception:
                    print("   Aviso: Pagina vazia ou layout invalido. A fechar e a prosseguir...")
                    aba_produtos.close()
                    continue
                
                # Pega os 5 primeiros produtos para analise
                itens = aba_produtos.locator(".ui-search-layout__item").all()[:5]
                
                print("   Produtos encontrados:")
                for item in itens:
                    # Tenta pegar o nome usando o novo layout (Polycard) ou o antigo como fallback
                    tag_nome = item.locator(".poly-component__title").first
                    if tag_nome.count() == 0:
                        tag_nome = item.locator(".ui-search-item__title").first
                        if tag_nome.count() == 0:
                            continue
                            
                    nome = tag_nome.text_content()
                    
                    try:
                        # Extrai o preco atual com seguranca (evita pegar o preco riscado)
                        tag_fracao = item.locator(".poly-price__current .andes-money-amount__fraction").first
                        tag_centavos = item.locator(".poly-price__current .andes-money-amount__cents").first
                        
                        if tag_fracao.count() == 0:
                            tag_fracao = item.locator(".andes-money-amount__fraction").first
                            tag_centavos = item.locator(".andes-money-amount__cents").first
                            
                        fracao = tag_fracao.text_content().replace(".", "")
                        centavos = tag_centavos.text_content() if tag_centavos.count() > 0 else "00"
                        preco_original = float(f"{fracao}.{centavos}")
                        
                    except Exception:
                        continue
                    
                    preco_final = preco_original
                    
                    # Matematica do cupom baseada nas regras de limite e tipo
                    if preco_original >= regra['min_amount']:
                        if regra['discount_type'] == 'PERCENT':
                            valor_desc = preco_original * (regra['discount_value'] / 100)
                        else:
                            valor_desc = regra['discount_value']
                            
                        if regra['cap_amount'] > 0 and valor_desc > regra['cap_amount']:
                            valor_desc = regra['cap_amount']
                            
                        preco_final = preco_original - valor_desc
                    
                    print(f"      - {nome[:45]}...")
                    print(f"         Preco: R$ {preco_original:.2f} -> Com Cupom: R$ {preco_final:.2f}")
                    
                    # Salva os dados do produto na lista local
                    produtos_deste_cupom.append({
                        "nome": nome,
                        "preco_original": preco_original,
                        "preco_final": preco_final,
                        "desconto_aplicado": round(preco_original - preco_final, 2)
                    })

                aba_produtos.close() 

                # Salva o resultado consolidado deste cupom na lista final
                resultados_finais.append({
                    "titulo_cupom": titulo,
                    "regras": regra,
                    "link_aplicacao": link_produtos,
                    "produtos_amostra": produtos_deste_cupom
                })

            except Exception as e:
                print(f"   Erro fatal neste cupom. Detalhe: {e}")
                if 'aba_produtos' in locals() and not aba_produtos.is_closed():
                    aba_produtos.close()

    return resultados_finais

if __name__ == "__main__":
    dados_extraidos = analisar_cupons_e_produtos()
    
    print("\n" + "="*50)
    print(f"Total de cupons processados com sucesso: {len(dados_extraidos)}")
    
    if dados_extraidos:
        with open("cupons_consolidados.json", "w", encoding="utf-8") as f:
            json.dump(dados_extraidos, f, ensure_ascii=False, indent=4)
        print("Dados salvos no arquivo 'cupons_consolidados.json'.")