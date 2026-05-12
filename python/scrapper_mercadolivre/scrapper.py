import sys
import os
import json
import re
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
                
            tracking_list = dados.get("appProps", {}).get("pageProps", {}).get("filteredCouponsData", {}).get("tracking", {}).get("view", {}).get("eventData", {}).get("coupons_list", [])            
            tracking_dict = {str(t.get("campaign_id")): t for t in tracking_list if "campaign_id" in t}

            for cupom in lista_da_pagina:
                camp_id = str(cupom.get("campaignId", ""))
                
                min_amount = cupom.get("minAmount")
                cap_amount = cupom.get("capAmount")
                fractional_amount = cupom.get("fractionalAmount")
                
                # Extrai o título de forma segura (lidando com strings ou dicionários)
                titulo_bruto = cupom.get("title", "Sem titulo")
                titulo_final = titulo_bruto.get("text") if isinstance(titulo_bruto, dict) else titulo_bruto
                subtitulo = cupom.get("initialSubtitle", {}).get("text", "")
                titulo_completo = f"{titulo_final} {subtitulo}".strip() if subtitulo else titulo_final
                
                track_info = tracking_dict.get(camp_id, {})
                segmentacoes = track_info.get("segmentations", {})

                acao = cupom.get("action", {})
                tipo_acao = acao.get("type")
                link_final = None

                # TIPO AÇÃO: LINK (Muitas vezes já vem com a URL pronta)
                if tipo_acao == "link" and acao.get("value"):
                    link_valor = acao.get("value")
                    if link_valor.startswith("http"):
                        link_final = link_valor
                    else:
                        link_final = f"https://www.mercadolivre.com.br{link_valor}"

                # TIPO AÇÃO: BUTTON (É aqui que a magia dos containers acontece)
                elif tipo_acao == "button" or not link_final:
                    
                    container_singular = segmentacoes.get("container", {})
                    container_lista = segmentacoes.get("containers", [])
                    
                    slug = ""
                    
                    if container_singular and container_singular.get("name"):
                        slug = str(container_singular.get("name"))
                    elif container_lista and container_lista[0].get("name"):
                        slug = str(container_lista[0].get("name"))
                    elif container_lista and container_lista[0].get("id"):
                        slug = str(container_lista[0].get("id"))
                    
                    created_by = track_info.get("created_by", "")
                    
                    # Identifica se é um cupom de Vendedor/Loja
                    is_seller = (created_by == "seller" and subtitulo.startswith("Em produtos de "))
                    
                    if is_seller:
                        nome_loja = subtitulo.replace("Em produtos de ", "").strip()
                        # Formata o nome para caso precisemos usar a URL raiz da loja
                        nome_loja_formatado = nome_loja.lower().replace(" oficial", "").replace(" ", "-")
                        
                        if slug:
                            # SE TEM CONTAINER (Ex: Luunaticos): NÃO usamos a raiz da loja. 
                            # O ML resolve o container direto, assim evitamos erros como Zebrands vs Luuna.
                            slug_formatado = slug.strip().replace(" ", "-")
                            link_final = f"https://lista.mercadolivre.com.br/_Container_{slug_formatado}"
                        else:
                            # SE NÃO TEM CONTAINER (Ex: Arno): Aqui sim usamos a URL na raiz da loja.
                            link_final = f"https://lista.mercadolivre.com.br/loja/{nome_loja_formatado}/"
                            
                    elif slug:
                        # Cupons normais do ML (Ex: Intel, Copa, Pet Shop)
                        slug_formatado = slug.strip().replace(" ", "-").lower()
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{slug_formatado}"
                    
                    elif segmentacoes.get("store_ids"):
                        loja_id = segmentacoes["store_ids"][0]
                        link_final = f"https://lista.mercadolivre.com.br/_CustId_{loja_id}"
                        
                    elif segmentacoes.get("categories"):
                        cat_id = segmentacoes["categories"][0].get("id")
                        link_final = f"https://lista.mercadolivre.com.br/{cat_id}"
                        
                    else:
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{camp_id}"

                    # Sempre adicionamos a campanha para atribuir o desconto
                    if link_final and "coupon_campaign_id" not in link_final:
                        conector = "&" if "?" in link_final else "?"
                        link_final += f"{conector}coupon_campaign_id={camp_id}"
                        

                if link_final:
                    link_final = link_final.replace("\u002F", "/").replace("\\/", "/")

                desconto = None
                if titulo_completo:
                    match_percent = re.search(r'(\d+(?:[.,]\d+)?)\s*%', titulo_completo)
                    match_fixed = re.search(r'R\$\s*(\d+(?:[.,]\d+)?)', titulo_completo, re.IGNORECASE)
                    if match_percent:
                        desconto = {
                            "valor": float(match_percent.group(1).replace(',', '.')),
                            "tipo": "porcentagem"
                        }
                    elif match_fixed:
                        desconto = {
                            "valor": float(match_fixed.group(1).replace(',', '.')),
                            "tipo": "fixo"
                        }

                cupom_limpo = {
                    "campaignId": camp_id,
                    "title": titulo_completo.replace("  ", " ").strip(),
                    "desconto": desconto,
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