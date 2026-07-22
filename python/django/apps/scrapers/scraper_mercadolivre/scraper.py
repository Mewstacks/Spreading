import os
import json
import re
import time
import logging
from contextlib import contextmanager

import requests
caminho_atual = os.path.dirname(os.path.abspath(__file__))
from apps.scrapers.auxiliar import iniciar_browser, BrowserError, SessaoExpirada, ua_aleatorio
from apps.scrapers.coupon_rules import derivar_categoria_cupom
from apps.scrapers.models import Cupom, Produto, CupomNormalizado, FonteIngestao
from apps.scrapers.progresso import emitir_progresso, emitir_fase
from apps.scrapers.session_paths import ml_auth_path
from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)


_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)


def _parse_payload_cupons(html_text):
    """Extrai o ``filteredCouponsData`` (JÁ DESEMBRULHADO) de um HTML/string de script.

    Serve os dois transportes: o texto do GET HTTP e o ``page.content()`` do browser.
    O ML alterna entre script com id, script JSON e bundle inline. Todos carregam a
    mesma chave ``filteredCouponsData``; a validação é por estrutura, não por uma
    substring fixa que some a cada deploy do site.

    Devolve o payload já desembrulhado. Antes devolvia a raiz: quem consumia só sabia
    descer por ``appProps``, então no formato achatado a extração dava certo mas o
    consumidor via lista vazia e a raspagem terminava em zero cupom sem dizer por quê.
    """
    if not html_text or "filteredCouponsData" not in html_text:
        return None
    # Sem BeautifulSoup no projeto: pega os corpos de <script> por regex e, como
    # rede de segurança, tenta também a string inteira (caminho browser/script cru).
    candidatos = _SCRIPT_RE.findall(html_text)
    candidatos.append(html_text)
    for text in candidatos:
        if "filteredCouponsData" not in text:
            continue
        # Formato Nordic legado: _n.ctx.r={...};_n.ctx.r.assets
        match = re.search(r"_n\.ctx\.r\s*=\s*(\{.*?\})\s*;\s*_n\.ctx\.r", text, re.S)
        blobs = [match.group(1)] if match else [text]
        for blob in blobs:
            try:
                data = json.loads(blob)
            except (TypeError, ValueError):
                continue
            current = data
            if isinstance(current, dict) and current.get("appProps"):
                current = current["appProps"].get("pageProps", {})
            if isinstance(current, dict) and isinstance(current.get("filteredCouponsData"), dict):
                return current["filteredCouponsData"]
    return None


def _extrair_payload_cupons(page):
    """Adapter do caminho browser: serializa o DOM e reusa o parser de string."""
    return _parse_payload_cupons(page.content())


_CUPONS_URL = ("https://www.mercadolivre.com.br/cupons/filter"
               "?all=true&source_page=int_view_all&page={n}")


def _ml_http_session(caminho_auth):
    """`requests.Session` com os cookies do storage_state do Playwright + headers de
    browser. Reusa a sessão já salva SEM subir Chromium — o payload de cupons é SSR,
    então um GET autenticado traz o mesmo `filteredCouponsData`. Cookies ausentes ou
    inválidos => o GET cai na página de login (sem payload) e o transporte alterna
    para o browser, que é quem sabe distinguir challenge de sessão expirada.
    """
    sess = requests.Session()
    try:
        with open(caminho_auth, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    for c in state.get("cookies", []):
        try:
            # Preserva o domínio como salvo (inclusive o ponto inicial): é ele que diz
            # ao jar para mandar o cookie também nos subdomínios (www., lista., ...).
            sess.cookies.set(c["name"], c["value"],
                             domain=c.get("domain") or "",
                             path=c.get("path") or "/")
        except Exception:
            continue
    sess.headers.update({
        "User-Agent": ua_aleatorio(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
    })
    return sess


@contextmanager
def _transporte_cupons(caminho_auth):
    """Fornece `fetch(n) -> str|None` para /cupons/filter, agnóstico ao transporte.

    Tenta HTTP primeiro (rápido, sem Chromium). O caller liga `estado['forcar_browser']`
    quando a 1ª página não traz payload (challenge do ML ou sessão morta); a partir daí
    o fetch serve via browser headless — que valida a sessão e levanta SessaoExpirada
    se o login caiu. O Chromium só é aberto se/quando o fallback ligar o flag.
    """
    session = _ml_http_session(caminho_auth)
    estado = {"forcar_browser": False, "usou_browser": False, "_cm": None, "_page": None}

    def fetch(n):
        if not estado["forcar_browser"]:
            try:
                resp = session.get(_CUPONS_URL.format(n=n), timeout=20)
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as e:
                logger.debug("Falha HTTP na pagina %s de cupons: %s", n, e)
                return None
        if estado["_page"] is None:
            cm = iniciar_browser(auth_path=caminho_auth, headless=True)
            page, _ctx = cm.__enter__()  # valida a sessão; SessaoExpirada propaga
            estado["_cm"] = cm
            estado["_page"] = page
            estado["usou_browser"] = True
        page = estado["_page"]
        try:
            page.goto(_CUPONS_URL.format(n=n))
            page.wait_for_load_state("domcontentloaded")
        except Exception as e:
            raise BrowserError(f"Nao foi possivel acessar a pagina de cupons: {e}")
        return page.content()

    try:
        yield fetch, estado
    finally:
        if estado["_cm"] is not None:
            estado["_cm"].__exit__(None, None, None)

def mapear_cupons(n=1, faixa=None):
    """Raspa /cupons/filter e popula a tabela Cupom. Retorna quantos foram salvos.

    Único caminho que preenche `Cupom`, e é dele que depende a geração de link de
    produto com campanha: link.py aborta quando o produto tem campanha_id e não há
    Cupom correspondente no banco. Ficou anos fora do loop automático, rodando só
    no clique manual da tela de Scraper — ver marketplaces/mercadolivre.scrape_all.

    Caminho rápido: GET HTTP autenticado (o payload é SSR no HTML), sem Chromium —
    derruba a raspagem de ~40 min para segundos. Só alterna para o browser (lento)
    quando o HTTP não traz payload na 1ª página (challenge do ML ou sessão expirada),
    pois é o browser que valida a sessão e levanta SessaoExpirada -> "reconecte".

    `faixa` (ini, fim) liga o progresso na tela; sem ela, o comportamento silencioso
    de antes (o ciclo automático não tem barra para alimentar).
    """
    todos_os_cupons_limpos = []
    MAX_RETRIES = 2
    RETRY_WAIT = 1.5  # s entre tentativas; sem browser não há corrida de render a esperar
    # Trava anti-runaway: o fim natural é `pagination.total` (ou a 1ª página vazia);
    # este teto só protege se ambos sumirem (formato mudou). Fica bem acima do catálogo
    # real (~72 páginas) para nunca cortar uma varredura legítima.
    MAX_PAGINAS = 200
    # O total de páginas é desconhecido (o laço vai até vir uma vazia), então a barra
    # não pode ser i/total: a fração se aproxima do fim da faixa sem nunca chegar.
    PAGINAS_TIPICAS = 8
    varredura_completa = False
    motivo_parada = ""

    caminho_auth = ml_auth_path()
    logger.info("Iniciando raspagem e limpeza de cupons")

    with _transporte_cupons(caminho_auth) as (fetch_html, _estado):
        while True:
            if n > MAX_PAGINAS:
                logger.warning("Limite de %s páginas atingido na varredura de cupons; encerrando", MAX_PAGINAS)
                motivo_parada = f"o limite de {MAX_PAGINAS} páginas foi atingido"
                break

            tentativa = 0
            dados = None
            emitir_fase(f"Cupons de campanha — página {n}",
                        min(n / PAGINAS_TIPICAS, 0.95), faixa)

            while tentativa < MAX_RETRIES:
                html = fetch_html(n)
                dados = _parse_payload_cupons(html) if html else None
                if dados is None:
                    logger.debug("Payload de cupons ausente na pagina %s, tentativa %s/%s", n, tentativa + 1, MAX_RETRIES)
                    tentativa += 1
                    time.sleep(RETRY_WAIT)
                    continue

                lista_check = dados.get("coupons", [])
                if len(lista_check) == 0:
                    logger.debug("Pagina %s retornou vazia, tentativa %s/%s", n, tentativa + 1, MAX_RETRIES)
                    tentativa += 1
                    time.sleep(RETRY_WAIT)
                    continue

                break  # sucesso

            if dados is None:
                if n == 1 and not _estado["forcar_browser"]:
                    # HTTP não trouxe payload logo na 1ª página: challenge do ML ou
                    # sessão morta. Liga o browser (que valida o login) e refaz a
                    # varredura do zero. Nada foi salvo ainda nesta página.
                    logger.info("HTTP sem payload na página 1; alternando para o browser")
                    _estado["forcar_browser"] = True
                    continue
                logger.warning("Pagina %s falhou apos %s tentativas; encerrando", n, MAX_RETRIES)
                motivo_parada = (
                    f"o payload de cupons não foi encontrado na página {n} "
                    f"(o formato do site pode ter mudado)")
                break

            lista_da_pagina = dados.get("coupons", [])
            if len(lista_da_pagina) == 0:
                logger.info("Pagina %s retornou vazia apos %s tentativas; fim da busca", n, MAX_RETRIES)
                # Página vazia é o fim natural da paginação: a varredura viu o
                # catálogo inteiro e só aqui é seguro expirar o que não apareceu.
                varredura_completa = True
                break

            tracking_list = dados.get("tracking", {}).get("view", {}).get("eventData", {}).get("coupons_list", [])
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

            logger.debug("Pagina %s processada: %s cupons limpos; total=%s", n, len(lista_da_pagina), len(todos_os_cupons_limpos))

            # O próprio payload diz quantas páginas existem: parar em `total` é o fim
            # natural (varredura completa) e evita depender da página vazia seguinte.
            total_paginas = dados.get("pagination", {}).get("total")
            if isinstance(total_paginas, int) and total_paginas > 0 and n >= total_paginas:
                logger.info("Última página (%s de %s) processada; varredura completa", n, total_paginas)
                varredura_completa = True
                break

            n += 1

    if todos_os_cupons_limpos:
        # A raspagem no browser leva vários minutos sem tocar o banco, então a
        # conexão persistente (conn_max_age=600) morre ociosa e o proxy do Fly a
        # derruba. CONN_HEALTH_CHECKS só revalida no início de cada request, não no
        # meio deste. Descarta a conexão stale para o bulk_create abrir uma nova.
        close_old_connections()
        cupons_db = [
            Cupom(
                campanha_id=c["campaignId"],
                titulo=c["title"],
                tipo_desconto=(c.get("desconto") or {}).get("tipo", ""),
                valor_desconto=(c.get("desconto") or {}).get("valor", 0.0),
                valor_minimo=c.get("valor_minimo") or 0.0,
                link_original=c.get("link_produtos") or "",
                codigo=c.get("codigo") or "",
                fonte="mercadolivre-cupom",
                ultima_verificacao=timezone.now(),
                estado="ativo",
            )
            for c in todos_os_cupons_limpos
        ]
        Cupom.objects.bulk_create(
            cupons_db,
            update_conflicts=True,
            unique_fields=["campanha_id"],
            update_fields=[
                "titulo", "tipo_desconto", "valor_desconto", "valor_minimo",
                "link_original", "codigo", "fonte", "ultima_verificacao", "estado",
            ],
        )
        ids_ativos = {c["campaignId"] for c in todos_os_cupons_limpos}
        # Expirar o que não apareceu só é válido se a varredura chegou ao fim. Quando
        # ela para no meio (payload some na página 3), o que veio é uma FATIA do
        # catálogo — e expirar "todo o resto" apagava a aba Cupons inteira por causa
        # de uma falha de rede. A guarda anti-wipe antiga só cobria o caso 100% vazio.
        if not varredura_completa:
            logger.warning(
                "Varredura de cupons parcial (%s cupom(ns) em %s página(s)): %s. "
                "Nada foi expirado.", len(todos_os_cupons_limpos), n - 1,
                motivo_parada or "interrompida")
        else:
            expirados = Cupom.objects.exclude(campanha_id__in=ids_ativos).update(
                estado="expirado", ultima_verificacao=timezone.now())
            if expirados:
                logger.info("%s cupom(ns) marcado(s) como expirado(s)", expirados)
            # Limpa apenas a lane de cupons do pool compartilhado do ML. Um exclude()
            # global aqui apagava também ofertas, buscas e produtos privados da Amazon.
            prods_expirados = (
                Produto.objects
                .filter(marketplace="mercadolivre", owner__isnull=True, origem="cupom")
                .exclude(campanha_id__in=ids_ativos)
                .update(
                    estado="expirado",
                    falha_verificacao="Cupom não observado na última sincronização",
                    ultima_verificacao=timezone.now(),
                )
            )
            if prods_expirados:
                logger.info("%s produto(s) de cupons marcados como expirados", prods_expirados)
        logger.info("%s cupons salvos/atualizados", len(todos_os_cupons_limpos))
    else:
        # Guarda anti-wipe: coleta vazia não expira nada (o bloco acima nem roda).
        # Mesmo contrato de mapear_cupons_codigo e mapear_ofertas.
        logger.warning("Raspagem de cupons de campanha ML vazia; nada alterado")
    if motivo_parada:
        # A tela precisa saber POR QUE veio pouco/nada. Sem esta linha o SSE dizia
        # apenas "0 cupom(ns)" e não havia como distinguir catálogo vazio de parser
        # quebrado. `_sse_runner` captura o stdout; no worker vira log.
        emitir_progresso(f"Aviso: a varredura de cupons parou — {motivo_parada}.")
    return len(todos_os_cupons_limpos)


def projetar_catalogo_cupons(faixa=None):
    """Projeta os cupons de campanha (tabela `Cupom`) para o catálogo público
    (`CupomNormalizado`) — a aba "Cupons" do site lê SÓ o `CupomNormalizado`.

    Sem isto, milhares de cupons de campanha válidos ficavam presos na tabela
    `Cupom` e a aba Cupons aparecia vazia: o único caminho que escrevia
    `CupomNormalizado` era o dos códigos digitáveis (`cupons_codigo_scraper`), e o
    ML parou de expor esses códigos em texto. Roda a cada scrape, lendo o banco —
    independe de a raspagem deste ciclo ter trazido cupom novo.
    """
    emitir_fase("Publicando os cupons na aba Cupons", 0.0, faixa)
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="mercadolivre-web", defaults={
            "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})

    ativos = list(Cupom.objects.filter(estado="ativo"))
    # Anti-wipe: sem cupom ativo, não desativa o catálogo (a coleta pode ter caído).
    if not ativos:
        logger.warning("Nenhum cupom de campanha ativo; catálogo de cupons preservado")
        return 0

    externos_vistos = set()
    for i, c in enumerate(ativos, 1):
        # São milhares de update_or_create: sem um pulso aqui a barra congelava na
        # última linha da etapa anterior por minutos.
        if faixa and (i % 50 == 0 or i == 1):
            emitir_fase(f"Publicando cupons {i}/{len(ativos)}", i / len(ativos), faixa)
        ext = f"campanha:{c.campanha_id}"
        externos_vistos.add(ext)
        if c.tipo_desconto == "fixo":
            resumo = f"R$ {c.valor_desconto:.2f} de desconto"
        elif c.tipo_desconto:
            resumo = f"{c.valor_desconto:.0f}% de desconto"
        else:
            resumo = ""
        titulo = c.titulo or (f"Cupom — {resumo}" if resumo else "Cupom Mercado Livre")
        regras = {"tipo_desconto": (
                        "porcentagem" if c.tipo_desconto == "percentual"
                        else c.tipo_desconto),
                  "valor_desconto": c.valor_desconto,
                  "valor_minimo": c.valor_minimo,
                  "desconto_maximo": None,
                  "modo_resgate": "ativacao",
                  "escopo": "",
                  "container_url": "",
                  "container_name": "",
                  "is_mar_aberto": False,
                  "dia_inicio": "", "dia_fim": ""}
        CupomNormalizado.objects.update_or_create(
            fonte=fonte, external_id=ext,
            defaults={
                "marketplace": "mercadolivre",
                "titulo": titulo[:255],
                # `code`/`inputCode` desta API e um token opaco de ativacao, nao
                # um codigo digitavel. Mantemos como evidencia e nunca o exibimos.
                "codigo": "",
                # Campanha nao traz escopo; deriva categoria p/ o filtro nao ficar vazio.
                "categoria": derivar_categoria_cupom(titulo, regras),
                "link": c.link_original or "https://www.mercadolivre.com.br/cupons",
                "validade": c.validade,
                "confianca": "media",
                "estado": "ativo",
                "regras": regras,
                "evidencia": {"transport": "public-web", "association": "campaign",
                              "token_ativacao": c.codigo or ""},
            },
        )

    # Sincroniza o catálogo com o estado do `Cupom`: campanhas que saíram do ar
    # deixam de aparecer. Só mexe nas projeções de campanha (external_id
    # "campanha:"), nunca nos cupons de checkout ("checkout:").
    obsoletos = (CupomNormalizado.objects
                 .filter(fonte=fonte, estado="ativo", external_id__startswith="campanha:")
                 .exclude(external_id__in=externos_vistos))
    n_obsoletos = obsoletos.update(estado="expirado")
    if n_obsoletos:
        logger.info("%s cupom(ns) de campanha expirado(s) no catálogo", n_obsoletos)
    logger.info("Catálogo de cupons: %s cupom(ns) de campanha projetado(s)", len(ativos))
    return len(ativos)


def listar_itens_por_cupom(cupom, page, max_paginas=5):
    link = cupom.get("link_produtos")

    if not link or link == "URL_NAO_MAPEADA":
        return None

    logger.debug("Acessando cupom: %s", cupom.get("title"))
    produtos_raspados = []
    
    try:
        page.goto(link)
    except Exception as e:
        logger.warning("Erro ao carregar a pagina do cupom: %s", e)
        return None

    pagina_atual = 1
    
    while pagina_atual <= max_paginas:
        try:
            page.wait_for_selector(".ui-search-layout", timeout=15000)
            page.wait_for_timeout(2000)
        except Exception:
            logger.debug("Pagina %s sem produtos encontrados ou demorou demais", pagina_atual)
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

                logger.debug("Dicionario de categorias criado com %s itens", len(categorias_por_id))
        except Exception:
            pass

        cards_produtos = page.locator(".ui-search-layout__item").all()
        logger.debug("Pagina %s: encontrados %s produtos", pagina_atual, len(cards_produtos))

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
                logger.debug("Erro isolado num produto: %s", e)
        
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
    logger.debug("%s produtos coletados para o cupom %s", len(produtos_raspados), cupom.get("campaignId", ""))
    return cupom_atualizado


def _sincronizar_produtos_no_banco(cupons_com_produtos):
    processados = 0
    for entrada in cupons_com_produtos:
        camp_id = entrada.get("campaignId", "")
        Produto.objects.filter(
            marketplace="mercadolivre",
            owner__isnull=True,
            origem="cupom",
            campanha_id=camp_id,
        ).update(
            estado="stale",
            falha_verificacao="Substituído por nova sincronização do cupom",
            ultima_verificacao=timezone.now(),
        )
        produtos = entrada.get("produtos_aplicaveis", [])
        if produtos:
            Produto.objects.bulk_create([
                Produto(
                    campanha_id=camp_id,
                    fonte="mercadolivre-cupom",
                    nome=p["nome_produto"][:255],
                    preco_sem_desconto=float(p["preco_vitrine_atual"]),
                    preco_com_cupom=float(p["preco_final_com_cupom"]),
                    preco_fonte=float(p["preco_vitrine_atual"]),
                    preco_efetivo=float(p["preco_final_com_cupom"]),
                    link_produto=p.get("link_produto") or "",
                    categoria=p.get("categoria", "DESCONHECIDO"),
                    marketplace="mercadolivre",
                    origem="cupom",
                    estado="ativo",
                    ultima_verificacao=timezone.now(),
                )
                for p in produtos
            ])
        processados += 1
    return processados


def main():
    mapear_cupons()
    # A aba Cupons lê o CupomNormalizado: projeta já aqui para os cupons aparecerem
    # mesmo quando este fluxo roda fora do ciclo automático (scrape_all).
    try:
        projetar_catalogo_cupons()
    except Exception as e:
        logger.warning("Projeção do catálogo de cupons falhou: %s", e)
    cupons_qs = Cupom.objects.all().values(
        "campanha_id", "titulo", "tipo_desconto", "valor_desconto", "valor_minimo", "link_original"
    )
    if not cupons_qs.exists():
        logger.warning("Nenhum cupom encontrado no banco")
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

    logger.info("%s cupons ja processados anteriormente; pulando", pulados)
    logger.info("%s cupons pendentes para raspar produtos", len(cupons_pendentes))

    if not cupons_pendentes:
        logger.info("Nada a fazer; todos os cupons ja tem produtos")
        return

    caminho_auth = ml_auth_path()

    resultados_pendentes = []
    total = len(cupons_pendentes)
    with iniciar_browser(auth_path=caminho_auth, headless=True) as (page, context):
        for i, cupom in enumerate(cupons_pendentes, 1):
            emitir_progresso(f"[PROGRESSO] Cupom {i}/{total} ({i*100//total}%)")
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
        logger.warning("Falha ao popular macro-categorias: %s", e)

    # Pré-gera links de afiliado em lote (1 sessão Playwright) para os produtos novos.
    # Assim o envio depois é instantâneo e não depende do browser na hora crítica.
    try:
        from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote
        novos = Produto.objects.filter(
            campanha_id__in=[c["campaignId"] for c in cupons_pendentes],
            link_afiliado="",
        )
        gerar_links_em_lote(list(novos))
    except SessaoExpirada as e:
        # Sessão morta pede ação humana (reconectar). Como warning isto passou
        # despercebido enquanto o lote inteiro falhava — daí o logger.error.
        # Não propaga: os cupons já foram processados e a pré-geração é só um
        # adiantamento; o link é gerado no envio se faltar.
        logger.error("Sessao ML expirada ao pre-gerar links: %s", e)
    except Exception as e:
        logger.warning("Falha ao pre-gerar links de afiliado: %s", e)

    logger.info("%s cupons processados", len(cupons_pendentes))

if __name__ == "__main__":
    main()
