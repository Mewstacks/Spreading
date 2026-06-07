import os
import json
import re
import time
caminho_atual = os.path.dirname(os.path.abspath(__file__))
from apps.scrapers.auxiliar import iniciar_browser, BrowserError
from apps.scrapers.models import Cupom, Produto

def mapear_cupons(n=1):
    todos_os_cupons_limpos = []
    MAX_RETRIES = 3
    RETRY_WAIT = 5  # segundos entre tentativas

    caminho_auth = os.path.join(caminho_atual, "auth.json")
    print("Iniciando raspagem e limpeza de cupons...")
    
    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        while True:
            tentativa = 0
            dados = None

            while tentativa < MAX_RETRIES:
                try:
                    page.goto(f"https://www.mercadolivre.com.br/cupons/filter?all=true&source_page=int_view_all&page={n}")
                    page.wait_for_load_state("domcontentloaded")
                except Exception as e:
                    raise BrowserError(f"Nao foi possivel acessar a pagina de cupons: {e}")

                try:
                    page.wait_for_selector("#__NORDIC_RENDERING_CTX__", state="attached", timeout=15000)
                except Exception:
                    print(f"Tag nao encontrada na pagina {n}, tentativa {tentativa + 1}/{MAX_RETRIES}.")
                    tentativa += 1
                    time.sleep(RETRY_WAIT)
                    continue

                texto_script = page.locator("#__NORDIC_RENDERING_CTX__").text_content()

                try:
                    json_puro = texto_script.split('_n.ctx.r=')[1].split(';_n.ctx.r.assets')[0]
                    dados = json.loads(json_puro)
                except Exception as e:
                    print(f"Erro ao ler JSON na pagina {n}, tentativa {tentativa + 1}/{MAX_RETRIES}: {e}")
                    tentativa += 1
                    time.sleep(RETRY_WAIT)
                    continue

                lista_check = dados.get("appProps", {}).get("pageProps", {}).get("filteredCouponsData", {}).get("coupons", [])
                if len(lista_check) == 0:
                    print(f"Pagina {n} retornou vazia, tentativa {tentativa + 1}/{MAX_RETRIES}. Aguardando {RETRY_WAIT}s...")
                    tentativa += 1
                    time.sleep(RETRY_WAIT)
                    continue

                break  # sucesso

            if dados is None:
                print(f"Pagina {n}: falhou apos {MAX_RETRIES} tentativas. Encerrando.")
                break

            lista_da_pagina = dados.get("appProps", {}).get("pageProps", {}).get("filteredCouponsData", {}).get("coupons", [])
            if len(lista_da_pagina) == 0:
                print(f"Pagina {n} retornou vazia apos {MAX_RETRIES} tentativas. Fim da busca!")
                break
                
            tracking_list = dados.get("appProps", {}).get("pageProps", {}).get("filteredCouponsData", {}).get("tracking", {}).get("view", {}).get("eventData", {}).get("coupons_list", [])            
            tracking_dict = {str(t.get("campaign_id")): t for t in tracking_list if "campaign_id" in t}

            for cupom in lista_da_pagina:
                camp_id = str(cupom.get("campaignId", ""))
                titulo_bruto = cupom.get("title", "Sem titulo")
                titulo_final = titulo_bruto.get("text") if isinstance(titulo_bruto, dict) else titulo_bruto
                subtitulo = cupom.get("initialSubtitle", {}).get("text", "")
                titulo_completo = f"{titulo_final} {subtitulo}".strip() if subtitulo else titulo_final
                
                track_info = tracking_dict.get(camp_id, {})
                segmentacoes = track_info.get("segmentations", {})

                # Padrões de URL que NÃO são páginas de listagem/oferta
                _URLS_INVALIDAS = ("/social/", "/perfil/", "/usuario/", "/noindex/")

                acao = cupom.get("action", {})
                tipo_acao = acao.get("type")
                link_final = None

                if tipo_acao == "link" and acao.get("value"):
                    link_valor = acao.get("value")
                    candidato = link_valor if link_valor.startswith("http") else f"https://www.mercadolivre.com.br{link_valor}"
                    # Só aceita se for uma página de listagem — perfis sociais não têm promoções
                    if not any(p in candidato for p in _URLS_INVALIDAS):
                        link_final = candidato
                    # Se inválido, cai no elif abaixo para tentar reconstruir pelo container

                if tipo_acao == "button" or not link_final:
                    
                    container_singular = segmentacoes.get("container", {})
                    container_lista = segmentacoes.get("containers", [])
                    
                    slug = ""
                    
                    if container_singular and container_singular.get("name"):
                        slug = str(container_singular.get("name"))
                    elif container_lista:
                        c0 = container_lista[0]
                        if isinstance(c0, dict):
                            slug = str(c0.get("name") or c0.get("id") or "")
                        else:
                            slug = str(c0)
                    
                    created_by = track_info.get("created_by", "")
                    is_seller = (created_by == "seller" and subtitulo.startswith("Em produtos de "))
                    
                    if is_seller:
                        nome_loja = subtitulo.replace("Em produtos de ", "").strip()
                        nome_loja_formatado = nome_loja.lower().replace(" oficial", "").replace(" ", "-")
                        # Para cupons de vendedor, _Container_{seller_internal_id} redireciona
                        # para /social/ no ML — usa sempre a URL da loja, que é estável.
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
                        cat0 = segmentacoes["categories"][0]
                        cat_id = cat0.get("id") if isinstance(cat0, dict) else cat0
                        link_final = f"https://lista.mercadolivre.com.br/{cat_id}"
                        
                    else:
                        link_final = f"https://lista.mercadolivre.com.br/_Container_{camp_id}"

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

                codigo = cupom.get("code") or cupom.get("inputCode") or ""

                # Extrai valor mínimo de compra (ex: Compra mínima R$399 → 399.0)
                valor_minimo = 0.0
                frac_minimo = (cupom.get("amount") or {}).get("accessibility", {}).get("minAmount", {}).get("fractionalAmount", "")
                if frac_minimo:
                    try:
                        valor_minimo = float(str(frac_minimo).replace(",", "."))
                    except ValueError:
                        pass

                cupom_limpo = {
                    "campaignId": camp_id,
                    "title": titulo_completo.replace("  ", " ").strip(),
                    "desconto": desconto,
                    "link_produtos": link_final,
                    "codigo": codigo,
                    "valor_minimo": valor_minimo,
                }
                
                todos_os_cupons_limpos.append(cupom_limpo)

            print(f"Pagina {n} processada: {len(lista_da_pagina)} cupons limpos. (Total acumulado: {len(todos_os_cupons_limpos)})")
            n += 1

    if todos_os_cupons_limpos:
        cupons_db = [
            Cupom(
                campanha_id=c["campaignId"],
                titulo=c["title"],
                tipo_desconto=(c.get("desconto") or {}).get("tipo", ""),
                valor_desconto=(c.get("desconto") or {}).get("valor", 0.0),
                valor_minimo=c.get("valor_minimo") or 0.0,
                link_original=c.get("link_produtos") or "",
                codigo=c.get("codigo") or "",
            )
            for c in todos_os_cupons_limpos
        ]
        Cupom.objects.bulk_create(
            cupons_db,
            update_conflicts=True,
            unique_fields=["campanha_id"],
            update_fields=["titulo", "tipo_desconto", "valor_desconto", "valor_minimo", "link_original", "codigo"],
        )
        ids_ativos = {c["campaignId"] for c in todos_os_cupons_limpos}
        removidos, _ = Cupom.objects.exclude(campanha_id__in=ids_ativos).delete()
        if removidos:
            print(f"{removidos} cupom(ns) inativo(s) removido(s) do banco.")
        prods_removidos, _ = Produto.objects.exclude(campanha_id__in=ids_ativos).delete()
        if prods_removidos:
            print(f"{prods_removidos} produto(s) de cupons inativos removido(s) do banco.")
        print(f"DB: {len(todos_os_cupons_limpos)} cupons salvos/atualizados.")


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
            print(f"Página {pagina_atual} sem produtos encontrados ou demorou demais.")
            break

        categorias_por_id = {}
        try:
            tag = page.locator("#__NORDIC_RENDERING_CTX__")
            if tag.count() > 0:
                texto = tag.text_content()
                json_puro = texto.split('_n.ctx.r=')[1].split(';_n.ctx.r.assets')[0]
                dados = json.loads(json_puro)

                _item_id_para_cat = {}
                _product_id_para_item = {}

                def _buscar_domain_ids(obj):
                    if isinstance(obj, dict):
                        domain_id = obj.get("domain_id", "")
                        if domain_id:
                            item_id = obj.get("id")
                            if item_id and re.match(r'MLB[A-Z]?\d+', str(item_id)):
                                _item_id_para_cat[str(item_id)] = domain_id.replace("MLB-", "")

                        prod_id = obj.get("product_id")
                        item_id_ref = obj.get("item_id")
                        if prod_id and item_id_ref and re.match(r'MLB[A-Z]?\d+', str(prod_id)):
                            _product_id_para_item[str(prod_id)] = str(item_id_ref)
                        cat_prod_id = obj.get("catalog_product_id")
                        if cat_prod_id and item_id_ref and re.match(r'MLB[A-Z]?\d+', str(cat_prod_id)):
                            _product_id_para_item[str(cat_prod_id)] = str(item_id_ref)

                        for v in obj.values():
                            _buscar_domain_ids(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            _buscar_domain_ids(item)

                _buscar_domain_ids(dados)

                categorias_por_id.update(_item_id_para_cat)
                for pid, iid in _product_id_para_item.items():
                    if iid in _item_id_para_cat:
                        categorias_por_id[pid] = _item_id_para_cat[iid]

                print(f"[DEBUG] Dicionário de categorias criado com {len(categorias_por_id)} itens.")
        except Exception:
            pass

        cards_produtos = page.locator(".ui-search-layout__item").all()
        print(f"Página {pagina_atual}: Encontrados {len(cards_produtos)} produtos.")

        for card in cards_produtos:
            try:
                loc_nome = card.locator(".ui-search-item__title, .poly-component__title").first
                nome = loc_nome.inner_text(timeout=2000)

                loc_link = card.locator("a.ui-search-link, h2 a, h3 a").first
                link_prod = loc_link.get_attribute("href", timeout=2000)

                categoria = "DESCONHECIDO"

                # Plano A: data-id do card (padrão)
                prod_id = card.get_attribute("data-id")

                # Plano B: atributo data-head-id em componentes de preço (layouts novos)
                if not prod_id:
                    bloco_id = card.locator("[data-head-id]")
                    if bloco_id.count() > 0:
                        prod_id = bloco_id.first.get_attribute("data-head-id")

                # Plano C: parâmetros wid / item_id na URL do anúncio
                if (not prod_id or not str(prod_id).startswith("MLB")) and link_prod:
                    match_wid = re.search(r'[?&](?:wid|item_id)=(MLB\d+)', link_prod)
                    if match_wid:
                        prod_id = match_wid.group(1)

                # Plano D: regex clássico no path da URL
                if (not prod_id or not str(prod_id).startswith("MLB")) and link_prod:
                    url_path = link_prod.split('?')[0]
                    m = re.search(r'/MLB-(\d{6,})', url_path)
                    if m:
                        prod_id = 'MLB' + m.group(1)
                    else:
                        m = re.search(r'(MLB[A-Z]?\d{6,})', url_path)
                        if m:
                            prod_id = m.group(1)

                if prod_id:
                    if not str(prod_id).startswith("MLB"):
                        prod_id = f"MLB{prod_id}"
                    categoria = categorias_por_id.get(prod_id, "DESCONHECIDO")

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
                        # Desconto fixo maior que o preço = dado inválido, ignora o produto
                        if valor_desc >= preco_atual_float:
                            continue
                        preco_com_cupom = preco_atual_float - valor_desc

                # Sanidade: preço final tem que ser positivo e desconto < 90%
                if preco_com_cupom <= 0 or preco_com_cupom >= preco_atual_float:
                    continue
                if preco_atual_float > 0 and ((preco_atual_float - preco_com_cupom) / preco_atual_float) >= 0.90:
                    continue

                # Descarta produto que não atinge o valor mínimo de compra do cupom
                valor_minimo_cupom = cupom.get("valor_minimo") or 0
                if valor_minimo_cupom > 0 and preco_atual_float < valor_minimo_cupom:
                    continue

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
    print(f"{len(produtos_raspados)} produtos coletados para o cupom {cupom.get('campaignId', '')}.")
    return cupom_atualizado


def _sincronizar_produtos_no_banco(cupons_com_produtos):
    processados = 0
    for entrada in cupons_com_produtos:
        camp_id = entrada.get("campaignId", "")
        Produto.objects.filter(campanha_id=camp_id).delete()
        produtos = entrada.get("produtos_aplicaveis", [])
        if produtos:
            Produto.objects.bulk_create([
                Produto(
                    campanha_id=camp_id,
                    nome=p["nome_produto"][:255],
                    preco_sem_desconto=float(p["preco_vitrine_atual"]),
                    preco_com_cupom=float(p["preco_final_com_cupom"]),
                    link_produto=p.get("link_produto") or "",
                    categoria=p.get("categoria", "DESCONHECIDO"),
                )
                for p in produtos
            ])
        processados += 1
    return processados


def main():
    mapear_cupons()
    cupons_qs = Cupom.objects.all().values(
        "campanha_id", "titulo", "tipo_desconto", "valor_desconto", "valor_minimo", "link_original"
    )
    if not cupons_qs.exists():
        print("Nenhum cupom encontrado no banco. Deu merda")
        return

    # Cupons que já têm produtos scraped — podem ser pulados
    ja_feitos = set(
        Produto.objects.values_list("campanha_id", flat=True).distinct()
    )

    cupons_pendentes = []
    pulados = 0
    for c in cupons_qs:
        if c["campanha_id"] in ja_feitos:
            pulados += 1
            continue
        desconto = None
        if c["tipo_desconto"] and c["valor_desconto"]:
            desconto = {"tipo": c["tipo_desconto"], "valor": c["valor_desconto"]}
        cupons_pendentes.append({
            "campaignId": c["campanha_id"],
            "title": c["titulo"],
            "desconto": desconto,
            "link_produtos": c["link_original"],
            "valor_minimo": c.get("valor_minimo") or 0.0,
        })

    print(f"{pulados} cupons já processados anteriormente — pulando.")
    print(f"{len(cupons_pendentes)} cupons pendentes para raspar produtos...")

    if not cupons_pendentes:
        print("Nada a fazer. Todos os cupons já têm produtos.")
        return

    caminho_auth = os.path.join(caminho_atual, "auth.json")

    resultados_pendentes = []
    total = len(cupons_pendentes)
    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        for i, cupom in enumerate(cupons_pendentes, 1):
            print(f"[PROGRESSO] Cupom {i}/{total} ({i*100//total}%)")
            resultado = listar_itens_por_cupom(cupom, page)
            if resultado:
                resultados_pendentes.append(resultado)

    # Sincroniza com o banco FORA do contexto Playwright para evitar
    # SynchronousOnlyOperation (Playwright tem event loop interno que
    # conflita com Django ORM síncrono)
    if resultados_pendentes:
        _sincronizar_produtos_no_banco(resultados_pendentes)

    # Classifica produtos em macro-categorias (necessário para seleção por nicho)
    try:
        from apps.scrapers.scraper_mercadolivre.cateorize import popular_macro_categorias
        popular_macro_categorias()
    except Exception as e:
        print(f"[main] Falha ao popular macro-categorias: {e}")

    # Pré-gera links de afiliado em lote (1 sessão Playwright) para os produtos novos.
    # Assim o envio depois é instantâneo e não depende do browser na hora crítica.
    try:
        from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote
        novos = Produto.objects.filter(
            campanha_id__in=[c["campaignId"] for c in cupons_pendentes],
            link_afiliado="",
        )
        gerar_links_em_lote(list(novos))
    except Exception as e:
        print(f"[main] Falha ao pré-gerar links de afiliado: {e}")

    print(f"\nFinalizado! {len(cupons_pendentes)} cupons processados.")

if __name__ == "__main__":
    main()