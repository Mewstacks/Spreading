import sys
import os
import re
caminho_atual = os.path.dirname(os.path.abspath(__file__))
caminho_django = os.path.dirname(os.path.dirname(os.path.dirname(caminho_atual)))
sys.path.append(caminho_django)
from apps.scrapers.auxiliar import iniciar_browser, BrowserError
from apps.scrapers.session_paths import ml_session_dir


def _auth_path(usuario=None) -> str:
    """auth.json do ML. Por usuário (auth_{id}.json) se existir; senão o global."""
    if usuario is not None and getattr(usuario, "id", None):
        p = os.path.join(ml_session_dir(), f"auth_{usuario.id}.json")
        if os.path.exists(p):
            return p
    return os.path.join(ml_session_dir(), "auth.json")


class LoginError(Exception):
    """Exceção personalizada para erros de login."""
    pass

class AuthError(Exception):
    """Exceção personalizada para erros de autenticação, quando o ML bloqueia essa merda."""
    pass





class UrlNaoPermitidaError(Exception):
    """O Programa de Afiliados rejeitou a URL (ex: páginas /up/MLBU... de catálogo)."""
    pass


def _validar_resultado_link(bruto):
    """
    O Link Builder escreve no clipboard tanto o link quanto mensagens de erro
    (ex '⚠️ Este URL não é permitido pelo Programa.'). Só aceita http(s) real.
    Levanta UrlNaoPermitidaError quando o ML rejeita a URL.
    """
    s = (bruto or "").strip()
    if not s:
        raise ValueError("Link Builder não retornou nada (clipboard vazio).")
    baixo = s.lower()
    if "não é permitido" in baixo or "nao e permitido" in baixo or "não permitido" in baixo:
        raise UrlNaoPermitidaError(s)
    if not s.startswith("http"):
        raise ValueError(f"Resultado não é uma URL: {s[:120]}")
    return s


def link_tem_tag_afiliado(link_curto: str, usuario=None) -> bool:
    """
    A3 — Verifica que o link de afiliado realmente carrega a tag de afiliado do
    usuário (ou a global). Segue UM hop do encurtador (meli.la -> destino) e procura
    a tag OU os parâmetros de tracking do Programa (matt_word/matt_tool/tracking_id)
    na URL final. Sem isso, a venda não gera comissão.

    Falha de rede -> None tratado como False pelo chamador (não confia cegamente).
    Se a tag não estiver configurada, cai para a presença dos params padrão.
    """
    import requests as _requests
    from apps.scrapers.afiliado import tag_ml

    if not link_curto:
        return False
    tag = tag_ml(usuario)
    try:
        r = _requests.get(link_curto, allow_redirects=True, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        cadeia = " ".join([h.headers.get("location", "") for h in r.history] + [r.url])
    except Exception:
        return False

    baixo = cadeia.lower()
    if tag and tag.lower() in baixo:
        return True
    # Fallback: parâmetros de tracking que o Link Builder injeta no destino afiliado.
    return any(p in baixo for p in ("matt_word=", "matt_tool=", "tracking_id=", "forceinapp"))


def afiliate_link_builder(link_base, auth_path=None):
    with iniciar_browser(
        auth_path=auth_path or os.path.join(ml_session_dir(), "auth.json"),
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
            link_final = _validar_resultado_link(page.evaluate("navigator.clipboard.readText()"))

            print(f"Sucesso! O link gerado foi: {link_final}")
            return link_final

        except UrlNaoPermitidaError:
            print("URL rejeitada pelo Programa de Afiliados (pagina nao afiliavel).")
            return None
        except Exception as e:
            print(f"Erro ao gerar o link de afiliado: {e}")
            return None


def _extrair_item_id(url: str):
    """
    Extrai o item id MLB<digitos> de qualquer forma de URL do ML:
    click1/mclics (pdp_filters=item_id:MLB...), ?item_id=, ?wid=, ou /MLB-123 no path.
    Ignora catálogo MLBU (não é item afiliável). Retorna 'MLB123...' ou None.
    """
    from urllib.parse import unquote
    dec = unquote(url)
    for padrao in (r'item_id[:=]\s*(MLB\d+)', r'[?&]wid=(MLB\d+)', r'/MLB-?(\d{6,})'):
        m = re.search(padrao, dec)
        if m:
            g = m.group(1)
            return g if g.startswith("MLB") else f"MLB{g}"
    return None


def _montar_url_isca(url_produto: str, camp_id: str):
    """
    Normaliza a URL do produto para o formato canônico aceito pelo Programa de
    Afiliados e injeta coupon_campaign_id. URLs de tracking (click1/mclics),
    catálogo (/up/MLBU) ou perfis são reduzidas ao item MLB real quando possível.
    Retorna None se não for página de produto afiliável.
    """
    if not url_produto:
        return None

    _URLS_PROIBIDAS = ("/social/", "/perfil/", "/usuario/", "/noindex/")
    eh_tracking = "click1.mercadolivre" in url_produto or "/mclics/" in url_produto
    eh_catalogo_up = "/up/" in url_produto  # /up/MLBU... = catálogo, Programa rejeita
    url_limpa = url_produto.split('#')[0]

    if any(p in url_limpa for p in _URLS_PROIBIDAS):
        return None

    if eh_tracking or eh_catalogo_up:
        # Tracking/ads ou catálogo /up/: extrai o item MLB real (wid/item_id/path) e
        # reconstrói uma URL de produto afiliável. (wid costuma estar no fragment.)
        item_id = _extrair_item_id(url_produto)
        if not item_id or item_id.startswith("MLBU"):
            return None  # só catálogo universal, sem item real
        base = f"https://produto.mercadolivre.com.br/MLB-{item_id[3:]}"
    else:
        # Já é página de produto válida (produto./MLB- ou /p/MLB catálogo): mantém
        base = url_limpa.split('?')[0]

    # Ofertas (sem camp_id) não injetam coupon_campaign_id
    if not camp_id:
        return base
    separador = "&" if "?" in base else "?"
    return f"{base}{separador}coupon_campaign_id={camp_id}"


def _afiliar_url_na_pagina(page, link_base: str):
    """
    Gera UM link de afiliado numa página do Link Builder já aberta/logada.
    Reutilizável para chamadas em lote sem reabrir o browser.
    """
    page.get_by_role("textbox", name="Insira 1 ou mais URLs").fill(link_base)
    page.get_by_role("button", name="Gerar").click()
    page.get_by_role("button", name="Copiar").click()
    return _validar_resultado_link(page.evaluate("navigator.clipboard.readText()"))


def gerar_links_em_lote(produtos):
    """
    Pré-gera e persiste link_afiliado/url_isca para uma lista de Produtos numa
    ÚNICA sessão Playwright. Pula produtos que já têm link em cache.

    Detecta expiração de sessão UMA vez no início (login visível -> LoginError),
    em vez de quebrar no meio do lote.

    Retorna: (qtd_gerados, qtd_falhas).
    """
    pendentes = [p for p in produtos if not p.link_afiliado]
    if not pendentes:
        return (0, 0)

    gerados = 0
    falhas = 0
    with iniciar_browser(
        auth_path=os.path.join(ml_session_dir(), "auth.json"),
        headless=True,
        permissions=['clipboard-read', 'clipboard-write'],
    ) as (page, context):
        try:
            page.goto("https://www.mercadolivre.com.br/afiliados/linkbuilder#hub")
        except Exception:
            raise AuthError("Não foi possível acessar o Link Builder. Verifique conexão/sessão.")

        if page.get_by_test_id("user_id").is_visible(timeout=10000):
            raise LoginError("Sessão expirada. Faça login e rode novamente.")

        total_lote = len(pendentes)
        print(f"[link-lote] Gerando links de afiliado para {total_lote} produtos...")
        for i, prod in enumerate(pendentes, 1):
            print(f"[PROGRESSO] Link {i}/{total_lote} ({i*100//total_lote}%)")
            # Ofertas do feed têm campanha_id vazio: _montar_url_isca trata isso e só
            # injeta coupon_campaign_id quando há campanha. Só pulamos quando a URL
            # não é afiliável (catálogo/perfil) -> None.
            url_isca = _montar_url_isca(prod.link_produto, prod.campanha_id)
            if not url_isca:
                falhas += 1
                continue
            try:
                link = _afiliar_url_na_pagina(page, url_isca)
                prod.url_isca = url_isca
                prod.link_afiliado = link
                prod.afiliado_ok = link_tem_tag_afiliado(link)
                prod.save(update_fields=["url_isca", "link_afiliado", "afiliado_ok"])
                gerados += 1
            except Exception as e:
                print(f"[link-lote] Falha em {prod.campanha_id or prod.link_produto[:40]}: {e}")
                falhas += 1

    print(f"[link-lote] {gerados} links gerados, {falhas} falhas.")
    return (gerados, falhas)


def _nome_bate(nome_esperado: str, corpo: str) -> bool:
    """True se tokens significativos (>3 chars) do nome aparecem no HTML da página."""
    if not nome_esperado:
        return True
    tokens = [t for t in re.findall(r"\w+", nome_esperado.lower()) if len(t) > 3]
    if not tokens:
        return True
    achados = sum(1 for t in tokens if t in corpo)
    # exige maioria dos tokens (≥60%) para confirmar que é o produto certo
    return achados >= max(1, int(len(tokens) * 0.6))


def verificar_link_afiliado(link_afiliado: str, screenshot_path: str = None,
                            nome_esperado: str = None, confiar_desconto: bool = False) -> dict:
    """
    Abre o link de afiliado num browser real, segue o redirect (meli.la -> destino)
    e checa se a oferta certa aparece com cupom/afiliado ativos.

    Aceita DOIS destinos válidos:
      - página de produto (produto.mercadolivre / /p/MLB / /MLB-id)
      - landing de afiliado /social/ que destaca o produto (com botão 'Ir para produto')
    Em ambos confirma: produto não inativo, preço visível e (se nome_esperado dado)
    que o nome do produto bate com a página.

    Retorna dict-relatório com ok/url_final/is_pagina_produto/is_landing_afiliado/
    cupom_detectado/preco_visivel/nome_confere/erros.
    """
    relatorio = {
        "ok": False, "url_final": None, "is_pagina_produto": False,
        "is_landing_afiliado": False, "cupom_detectado": False,
        "preco_visivel": None, "nome_confere": None, "erros": [],
    }
    if not link_afiliado:
        relatorio["erros"].append("link_afiliado vazio.")
        return relatorio

    with iniciar_browser(
        auth_path=os.path.join(ml_session_dir(), "auth.json"),
        headless=True,
    ) as (page, context):
        try:
            page.goto(link_afiliado, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            url_final = page.url
            relatorio["url_final"] = url_final

            relatorio["is_pagina_produto"] = (
                "produto.mercadolivre" in url_final
                or "/p/MLB" in url_final
                or bool(re.search(r"/MLB-?\d{6,}", url_final))
            )
            # Landing de afiliado que destaca o produto (storefront /social/)
            relatorio["is_landing_afiliado"] = "/social/" in url_final

            corpo = page.content().lower()

            termos_morto = ["anúncio pausado", "página não encontrada", "estoque indisponível"]
            if any(t in corpo for t in termos_morto):
                relatorio["erros"].append("Página indica anúncio inativo/inexistente.")

            # Cupom REAL: badge de cupom na página ("X% OFF com cupom", "cupom de R$").
            # NÃO usar só a palavra "cupom" — ela aparece no menu global "Cupons" (falso positivo).
            relatorio["cupom_detectado"] = bool(
                re.search(r"com\s+cupom|cupom\s+de\s+r?\$|%\s*off\s*com\s*cupom|aplicar\s+cupom", corpo)
            )
            # Preço antigo riscado SOMENTE no buybox do produto (não nos cards de vitrine
            # da /social/, que dariam falso positivo de outro item).
            try:
                relatorio["preco_riscado"] = page.locator(
                    ".ui-pdp-price s.andes-money-amount--previous, .ui-pdp-container s.andes-money-amount--previous"
                ).first.is_visible(timeout=2000)
            except Exception:
                relatorio["preco_riscado"] = False

            if nome_esperado is not None:
                relatorio["nome_confere"] = _nome_bate(nome_esperado, corpo)

            try:
                preco_el = page.locator(".andes-money-amount__fraction").first
                if preco_el.is_visible(timeout=5000):
                    relatorio["preco_visivel"] = "R$ " + preco_el.inner_text().strip()
            except Exception:
                pass

            if screenshot_path:
                try:
                    page.screenshot(path=screenshot_path, full_page=False)
                    relatorio["screenshot"] = screenshot_path
                except Exception as e:
                    relatorio["erros"].append(f"screenshot falhou: {e}")

            inativo = any("inativo" in e or "inexistente" in e for e in relatorio["erros"])
            nome_ok = relatorio["nome_confere"] is not False  # None (não checado) ou True
            destino_valido = relatorio["is_pagina_produto"] or relatorio["is_landing_afiliado"]
            desconto_real = relatorio["cupom_detectado"] or relatorio.get("preco_riscado")

            if confiar_desconto:
                # Ofertas: desconto já confirmado na raspagem (de/por). Basta afiliado
                # válido (produto OU vitrine /social/), produto certo e ativo.
                relatorio["ok"] = destino_valido and nome_ok and not inativo
            else:
                # Cupom: exige confirmar o desconto NA página do produto (não /social/).
                relatorio["ok"] = (
                    relatorio["is_pagina_produto"]
                    and bool(relatorio["preco_visivel"])
                    and nome_ok
                    and desconto_real
                    and not inativo
                )
                if relatorio["is_landing_afiliado"] and not relatorio["is_pagina_produto"]:
                    relatorio["erros"].append(
                        "Caiu na vitrine /social/ (afiliado ok, mas não dá pra confirmar o cupom do item)."
                    )
        except Exception as e:
            relatorio["erros"].append(f"Falha ao abrir link: {e}")

    return relatorio


def produto_vencedor_do_cupom(campanha_id: str):
    """
    Retorna o Produto com o maior desconto absoluto (R$) para um dado campanha_id.
    Exige que Django esteja configurado (DJANGO_SETTINGS_MODULE) antes de chamar.
    """
    from apps.scrapers.models import Produto
    return (
        Produto.objects
        .filter(campanha_id=campanha_id)
        .extra(select={"economia": "preco_sem_desconto - preco_com_cupom"})
        .order_by("-economia")
        .first()
    )


def gerar_link_afiliado_para_produto(produto, usuario=None):
    """
    Deep-link direto para o produto isca com coupon_campaign_id injetado na URL.
    Isso garante que o usuário caia exatamente no produto anunciado (não num
    container dinâmico que pode ter trocado a ordem) e o ML exibe a barra de
    cupom aplicável automaticamente.

    usuario != None (multi-tenant): gera com a SESSÃO do usuário (auth_{id}.json) e a
    tag DELE, cacheando em LinkAfiliadoUsuario. Sem sessão própria, cai no auth.json
    global (link válido, mas a comissão vai p/ a conta global — avisar o usuário a
    conectar o próprio ML). usuario == None: comportamento single-tenant antigo.

    Retorna:
        {
          "link_afiliado": "https://meli.la/...",
          "produto_nome":  "...",
          "preco_vitrine": 1299.90,
          "preco_com_cupom": 1169.91,
          "cupom_titulo":  "10% OFF em Smartphones",
          "url_isca": "https://produto.mercadolivre.com.br/...?coupon_campaign_id=...",
        }
        ou None em caso de falha.
    """
    from apps.scrapers.models import Cupom

    camp_id = produto.campanha_id if hasattr(produto, "campanha_id") else produto["campanha_id"]
    url_produto = produto.link_produto if hasattr(produto, "link_produto") else produto["link_produto"]

    # Ofertas (origem='oferta') não têm Cupom; cupom fica None
    cupom = None
    if camp_id:
        cupom = Cupom.objects.filter(campanha_id=camp_id).first()
        if cupom is None:
            print(f"[link] Cupom {camp_id} não encontrado no banco.")
            return None

    if not url_produto:
        print(f"[link] Produto sem link_produto salvo (campanha {camp_id}).")
        return None

    # ── Multi-tenant: link por usuário (sessão + tag dele), cacheado por (usuario, produto) ──
    if usuario is not None:
        from apps.scrapers.afiliado import link_cacheado, salvar_cache
        cache = link_cacheado(usuario, produto)
        if cache and cache.link_afiliado:
            return {
                "link_afiliado": cache.link_afiliado, "afiliado_ok": cache.afiliado_ok,
                "produto_nome": getattr(produto, "nome", ""),
                "preco_vitrine": getattr(produto, "preco_sem_desconto", 0),
                "preco_com_cupom": getattr(produto, "preco_com_cupom", 0),
                "cupom_titulo": cupom.titulo if cupom else "",
                "url_isca": cache.url_isca,
            }
        url_isca = _montar_url_isca(url_produto, camp_id)
        if not url_isca:
            print(f"[link] URL do produto inválida: {url_produto}")
            return None
        link_afiliado = afiliate_link_builder(url_isca, auth_path=_auth_path(usuario))
        if not link_afiliado:
            return None
        afiliado_ok = link_tem_tag_afiliado(link_afiliado, usuario=usuario)
        salvar_cache(usuario, produto, link_afiliado, url_isca, afiliado_ok)
        return {
            "link_afiliado": link_afiliado, "afiliado_ok": afiliado_ok,
            "produto_nome": getattr(produto, "nome", ""),
            "preco_vitrine": getattr(produto, "preco_sem_desconto", 0),
            "preco_com_cupom": getattr(produto, "preco_com_cupom", 0),
            "cupom_titulo": cupom.titulo if cupom else "",
            "url_isca": url_isca,
        }

    # Usa cache se já foi pré-gerado em lote
    cache_link = getattr(produto, "link_afiliado", "") if hasattr(produto, "link_afiliado") else ""
    cache_isca = getattr(produto, "url_isca", "") if hasattr(produto, "url_isca") else ""

    if cache_link:
        url_isca = cache_isca or _montar_url_isca(url_produto, camp_id)
        link_afiliado = cache_link
    else:
        url_isca = _montar_url_isca(url_produto, camp_id)
        if not url_isca:
            print(f"[link] URL do produto inválida: {url_produto}")
            return None
        print(f"[link] Gerando afiliado para: {url_isca}")
        link_afiliado = afiliate_link_builder(url_isca)
        if not link_afiliado:
            return None
        # Persiste no cache se for instância de modelo
        if hasattr(produto, "save"):
            produto.url_isca = url_isca
            produto.link_afiliado = link_afiliado
            produto.afiliado_ok = link_tem_tag_afiliado(link_afiliado)
            produto.save(update_fields=["url_isca", "link_afiliado", "afiliado_ok"])

    return {
        "link_afiliado": link_afiliado,
        "afiliado_ok": getattr(produto, "afiliado_ok", None) if hasattr(produto, "afiliado_ok")
                       else link_tem_tag_afiliado(link_afiliado),
        "produto_nome": produto.nome if hasattr(produto, "nome") else produto["nome"],
        "preco_vitrine": produto.preco_sem_desconto if hasattr(produto, "preco_sem_desconto") else produto["preco_sem_desconto"],
        "preco_com_cupom": produto.preco_com_cupom if hasattr(produto, "preco_com_cupom") else produto["preco_com_cupom"],
        "cupom_titulo": cupom.titulo if cupom else "",
        "url_isca": url_isca,
    }