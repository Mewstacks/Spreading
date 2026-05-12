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
                
                titulo_bruto = cupom.get("title", "Sem titulo")
                titulo_final = titulo_bruto.get("text") if isinstance(titulo_bruto, dict) else titulo_bruto
                subtitulo = cupom.get("initialSubtitle", {}).get("text", "")
                titulo_completo = f"{titulo_final} {subtitulo}".strip() if subtitulo else titulo_final
                
                track_info = tracking_dict.get(camp_id, {})
                segmentacoes = track_info.get("segmentations", {})

                acao = cupom.get("action", {})
                tipo_acao = acao.get("type")
                link_final = None

                if tipo_acao == "link" and acao.get("value"):
                    link_valor = acao.get("value")
                    if link_valor.startswith("http"):
                        link_final = link_valor
                    else:
                        link_final = f"https://www.mercadolivre.com.br{link_valor}"

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
                    is_seller = (created_by == "seller" and subtitulo.startswith("Em produtos de "))
                    
                    if is_seller:
                        nome_loja = subtitulo.replace("Em produtos de ", "").strip()
                        nome_loja_formatado = nome_loja.lower().replace(" oficial", "").replace(" ", "-")
                        
                        if slug:
                            slug_formatado = slug.strip().replace(" ", "-")
                            link_final = f"https://lista.mercadolivre.com.br/_Container_{slug_formatado}"
                        else:
                            link_final = f"https://lista.mercadolivre.com.br/loja/{nome_loja_formatado}/"
                            
                    elif slug:
                        slug_formatado = slug.strip().replace(" ", "-").lower()
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{slug_formatado}"
                    
                    elif segmentacoes.get("store_ids"):
                        loja_id = str(segmentacoes["store_ids"][0])
                        if loja_id == "-1":
                            link_final = "https://www.mercadolivre.com.br/l/lojas-oficiais#origin=coupons"
                        else:
                            link_final = f"https://lista.mercadolivre.com.br/_CustId_{loja_id}"
                        
                    elif segmentacoes.get("categories"):
                        cat_id = segmentacoes["categories"][0].get("id")
                        link_final = f"https://lista.mercadolivre.com.br/{cat_id}"
                        
                    else:
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{camp_id}"

                    if link_final and "coupon_campaign_id" not in link_final:
                        if "#" in link_final:
                            partes = link_final.split("#")
                            conector = "&" if "?" in partes[0] else "?"
                            link_final = f"{partes[0]}{conector}coupon_campaign_id={camp_id}#{partes[1]}"
                        else:
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


def listar_itens_por_cupom(cupom, page, max_paginas=5):
    link = cupom.get("link_produtos")

    if not link or link == "URL_NAO_MAPEADA":
        return None

    print(f"\n[+] Acessando cupom: {cupom['title']}")
    produtos_raspados = []
    
    try:
        page.goto(link)
    except Exception as e:
        print(f"Erro ao carregar a página do cupom: {e}")
        return None

    pagina_atual = 1
    
    while pagina_atual <= max_paginas:
        try:
            page.wait_for_selector(".ui-search-layout", timeout=15000)
            page.wait_for_timeout(2000)
        except Exception:
            print(f"    Página {pagina_atual} sem produtos encontrados ou demorou demais.")
            break

        categorias_por_id = {}
        try:
            tag = page.locator("#__NORDIC_RENDERING_CTX__")
            if tag.count() > 0:
                texto = tag.text_content()
                json_puro = texto.split('_n.ctx.r=')[1].split(';_n.ctx.r.assets')[0]
                dados = json.loads(json_puro)
                lista_json = dados.get("appProps", {}).get("pageProps", {}).get("results", [])
                for item in lista_json:
                    item_id = item.get("id")
                    polycard = item.get("polycard", {})
                    metadata = polycard.get("metadata", {})
                    meta_id = metadata.get("id")
                    domain_id = metadata.get("domain_id", "")
                    if domain_id:
                        categoria_limpa = domain_id.replace("MLB-", "")
                        if item_id:
                            categorias_por_id[item_id] = categoria_limpa
                        if meta_id:
                            categorias_por_id[meta_id] = categoria_limpa
        except Exception:
            pass

        cards_produtos = page.locator(".ui-search-layout__item").all()
        print(f"    Página {pagina_atual}: Encontrados {len(cards_produtos)} produtos.")

        for card in cards_produtos:
            try:
                loc_nome = card.locator(".ui-search-item__title, .poly-component__title").first
                nome = loc_nome.inner_text(timeout=2000)

                loc_link = card.locator("a.ui-search-link, h2 a, h3 a").first
                link_prod = loc_link.get_attribute("href", timeout=2000)

                categoria = "Desconhecido"
                if link_prod:
                    match_wid = re.search(r'[?&]wid=(MLB\d+)', link_prod)
                    if match_wid:
                        prod_id = match_wid.group(1)
                        categoria = categorias_por_id.get(prod_id, "Desconhecido")

                bloco_preco_atual_frac = card.locator(".ui-search-price__second-line .andes-money-amount__fraction, .poly-price__current .andes-money-amount__fraction")
                if bloco_preco_atual_frac.count() == 0:
                    continue

                frac_atual = bloco_preco_atual_frac.first.inner_text(timeout=2000).replace('.', '')
                bloco_preco_atual_cents = card.locator(".poly-price__current .andes-money-amount__cents, .ui-search-price__second-line .andes-money-amount__cents")
                cents_atual = bloco_preco_atual_cents.first.inner_text(timeout=2000) if bloco_preco_atual_cents.count() > 0 else "0"
                preco_atual_float = float(f"{frac_atual}.{cents_atual.zfill(2)}")

                bloco_preco_antigo = card.locator("s.andes-money-amount--previous .andes-money-amount__fraction")
                if bloco_preco_antigo.count() > 0:
                    frac_antigo = bloco_preco_antigo.first.inner_text(timeout=2000).replace('.', '')
                    bloco_antigo_cents = card.locator("s.andes-money-amount--previous .andes-money-amount__cents")
                    cents_antigo = bloco_antigo_cents.first.inner_text(timeout=2000) if bloco_antigo_cents.count() > 0 else "0"
                    preco_antigo_float = float(f"{frac_antigo}.{cents_antigo.zfill(2)}")
                else:
                    preco_antigo_float = preco_atual_float

                desconto = cupom.get("desconto")
                preco_com_cupom = preco_atual_float
                
                if desconto:
                    valor_desc = desconto.get("valor", 0)
                    if desconto.get("tipo") == "porcentagem":
                        preco_com_cupom = preco_atual_float * (1 - (valor_desc / 100))
                    elif desconto.get("tipo") == "fixo":
                        preco_com_cupom = preco_atual_float - valor_desc
                        if preco_com_cupom < 0:
                            preco_com_cupom = 0

                produtos_raspados.append({
                    "nome_produto": nome,
                    "categoria": categoria,
                    "link_produto": link_prod,
                    "preco_original_sem_desconto": f"{preco_antigo_float:.2f}",
                    "preco_vitrine_atual": f"{preco_atual_float:.2f}",
                    "preco_final_com_cupom": f"{preco_com_cupom:.2f}"
                })

            except Exception as e:
                print(f"Erro isolado num produto: {e}")
        
        seletores_prox = [
            ".andes-pagination__button--next:not(.andes-pagination__button--disabled) a",
            "a[title='Seguinte']",
            "li.andes-pagination__button--next a",
        ]
        navegou = False
        for seletor in seletores_prox:
            botao = page.locator(seletor)
            try:
                if botao.count() > 0 and botao.first.is_visible(timeout=2000):
                    href = botao.first.get_attribute("href")
                    if href:
                        page.goto(href)
                    else:
                        botao.first.click()
                        page.wait_for_load_state("domcontentloaded")
                    pagina_atual += 1
                    navegou = True
                    break
            except Exception:
                continue

        if not navegou:
            break

    cupom_atualizado = cupom.copy()
    cupom_atualizado["produtos_aplicaveis"] = produtos_raspados
    return cupom_atualizado


def main():
    json_path = os.path.join(caminho_atual, "cupons_mapeados.json")
    if not os.path.exists(json_path):
        print("Arquivo de cupons mapeados nao encontrado. Rode a funcao mapear_cupons() primeiro.")
        return

    with open(json_path, "r", encoding="utf-8") as f:
        cupons = json.load(f)

    caminho_auth = os.path.join(caminho_atual, "auth.json")
    cupons_com_produtos = []
    
    caminho_salvar = os.path.join(caminho_atual, "cupons_com_produtos_detalhados.json")

    print("Iniciando raspagem de produtos...")

    with iniciar_browser(auth_path=caminho_auth, headless=False) as (page, context):
        for cupom in cupons:
            resultado = listar_itens_por_cupom(cupom, page)
            
            if resultado:
                cupons_com_produtos.append(resultado)
                try:
                    with open(caminho_salvar, "w", encoding="utf-8") as f:
                        json.dump(cupons_com_produtos, f, ensure_ascii=False, indent=4)
                    print(f"Checkpoint salvo! {len(cupons_com_produtos)} cupons processados no JSON até agora.")
                except Exception as e:
                    print(f"Erro ao tentar salvar o arquivo JSON: {e}")

    print(f"\nFinalizado com sucesso! Todos os dados estão seguros em {caminho_salvar}")

if __name__ == "__main__":
    main()