import sys
import os
import json
caminho_atual = os.path.dirname(os.path.abspath(__file__))
caminho_raiz = os.path.dirname(caminho_atual)
sys.path.insert(0, caminho_raiz)
from auxiliar import iniciar_browser, BrowserError

def mapear_cupons(n=1):
    todos_os_cupons_limpos = [] 
    
    caminho_auth = os.path.join(caminho_atual, "auth.json")
    print("Iniciando raspagem e limpeza de cupons...")
    
    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        while True:
            try:
                page.goto(f"https://www.mercadolivre.com.br/cupons/filter?all=true&source_page=int_view_all&page={n}")
                page.wait_for_load_state("domcontentloaded")
            except Exception as e:
                raise BrowserError(f"Nao foi possivel acessar a pagina de cupons: {e}")
            
            try:
                page.wait_for_selector("#__NORDIC_RENDERING_CTX__", state="attached", timeout=15000)
            except Exception:
                print("Fim das paginas alcancado ou tag nao encontrada.")
                break 
            
            texto_script = page.locator("#__NORDIC_RENDERING_CTX__").text_content()
            
            try:
                json_puro = texto_script.split('_n.ctx.r=')[1].split(';_n.ctx.r.assets')[0]
                dados = json.loads(json_puro) 
            except Exception as e:
                print(f"Erro ao tentar limpar/ler o JSON na pagina {n}: {e}")
                break

            lista_da_pagina = dados.get("appProps", {}).get("pageProps", {}).get("filteredCouponsData", {}).get("coupons", [])
            
            if len(lista_da_pagina) == 0:
                print(f"Pagina {n} retornou vazia. Fim da busca!")
                break
                
            tracking_list = dados.get("appProps", {}).get("pageProps", {}).get("tracking", {}).get("view", {}).get("eventData", {}).get("coupons_list", [])
            
            tracking_dict = {str(t.get("campaign_id")): t for t in tracking_list if "campaign_id" in t}

            for cupom in lista_da_pagina:
                camp_id = str(cupom.get("campaignId", ""))
                
                min_amount = cupom.get("minAmount")
                cap_amount = cupom.get("capAmount")
                fractional_amount = cupom.get("fractionalAmount")
                
                acao = cupom.get("action", {})
                tipo_acao = acao.get("type")
                link_final = None
                
                if tipo_acao == "link":
                    link_final = acao.get("value")
                    
                elif tipo_acao == "button":
                    track_info = tracking_dict.get(camp_id, {})
                    segmentacoes = track_info.get("segmentations", {})
                    
                    if "containers" in segmentacoes and len(segmentacoes["containers"]) > 0:
                        container_id = segmentacoes["containers"][0].get("id")
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{container_id}"
                        
                    elif "store_ids" in segmentacoes and len(segmentacoes["store_ids"]) > 0:
                        loja_id = segmentacoes["store_ids"][0]
                        link_final = f"https://lista.mercadolivre.com.br/_CustId_{loja_id}"
                        
                    elif "categories" in segmentacoes and len(segmentacoes["categories"]) > 0:
                        cat_id = segmentacoes["categories"][0].get("id")
                        link_final = f"https://lista.mercadolivre.com.br/{cat_id}"
                        
                    else:
                        link_final = "URL_NAO_MAPEADA"

                cupom_limpo = {
                    "campaignId": camp_id,
                    "title": cupom.get("title", "Sem titulo"),
                    "minAmount": min_amount,
                    "capAmount": cap_amount,
                    "fractionalAmount": fractional_amount,
                    "tipo_acao": tipo_acao,
                    "link_produtos": link_final
                }
                
                todos_os_cupons_limpos.append(cupom_limpo)

            print(f"Pagina {n} processada: {len(lista_da_pagina)} cupons limpos. (Total acumulado: {len(todos_os_cupons_limpos)})")
            n += 1

    if len(todos_os_cupons_limpos) > 0:
        caminho_salvar = os.path.join(caminho_atual, "cupons_mapeados.json")
        with open(caminho_salvar, "w", encoding="utf-8") as f:
            json.dump(todos_os_cupons_limpos, f, ensure_ascii=False, indent=4)

if __name__ == "__main__":
    mapear_cupons(1)