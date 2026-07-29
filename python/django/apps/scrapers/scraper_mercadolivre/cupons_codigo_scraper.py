"""
Scraper da página oficial de cupons do ML: /ofertas/cupons.

Três saídas:
  1) Cupons do carrossel oficial -> CupomNormalizado com `container_url`. Esta é a
     saída PRINCIPAL: o vínculo com produtos é fechado depois por
     cupons_container.casar_cupons_container, que abre a lista do container.
  2) Produtos com desconto (de/por) -> Produto(origem='cupom_codigo'). O desconto
     é confirmado na PDP no momento do envio (confiar_desconto=False).
  3) Códigos digitáveis no checkout (ex: OFERTAS30, TECH10) extraídos da página ->
     upsert em CupomCodigo (lista global anexada às mensagens).

Por que (1) existe: a página foi redesenhada e os cupons deixaram de ser cards de
produto (`.poly-card`) — viraram componentes `smart-coupon` renderizados pelo
cliente, e no DOM já hidratado a área de produtos fica só com placeholders. Ou seja,
raspar o DOM devolvia ZERO cupons, e o formato "código digitável" que (3) procura
praticamente não existe mais nessa página (os cupons são de ATIVAÇÃO por clique; o
campo `code` do payload é um token opaco, não algo que o comprador digita). A fonte
de verdade é o JSON SSR embutido em `__NORDIC_RENDERING_CTX__`, que traz campanha,
desconto, mínimo, validade e a URL do container — que é justamente o que o
casamento cupom-produto precisa. (2) e (3) seguem como caminhos secundários porque
continuam valendo quando a página é aberta com sessão.
"""
import json
import os
import re
import logging

from django.utils.dateparse import parse_datetime

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.coupon_rules import derivar_categoria_cupom, rotulo_anunciante
from apps.scrapers.models import Produto, CupomCodigo, FonteIngestao, CupomNormalizado
from apps.scrapers.progresso import emitir_fase, emitir_progresso
from apps.scrapers.ml_auth import avisar_sem_sessao, storage_state
from apps.scrapers.scraper_mercadolivre.ofertas_scraper import _coletar_cards, _salvar

caminho_atual = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)

# Marca dos códigos criados por ESTE scraper (vs. curados à mão). Só desativamos/
# reativamos os automáticos; cupons curados manualmente ficam intocados.
_DESC_AUTO = "cupom ML (checkout)"

# Palavras maiúsculas comuns que NÃO são código de cupom (reduz falso-positivo do
# regex sobre texto livre da página).
_NAO_CODIGO = {"OFERTA", "OFERTAS", "VENDIDO", "VENDIDOS", "BREVE", "FRETE", "GRATIS",
               "FULL", "NOVO", "PIX", "OFF", "ATE", "MELI", "SUPER", "MEGA", "TOP",
               "ANDROID", "IPHONE", "SAMSUNG", "MOTOROLA", "XIAOMI", "LG", "SONY",
               "PS4", "PS5", "USB", "HD", "SSD", "GB", "TB", "TV", "LED", "LCD",
               "R$", "COMPRE", "LEVE", "GANHE", "ECONOMIZE"}


def _extrair_codigos(texto):
    """Códigos plausíveis: maiúsculas+dígitos (OFERTAS30, TECH10, FUTEBOL25)."""
    cands = set(re.findall(r"\b[A-Z]{3,}\d{1,3}\b", texto or ""))
    return [c for c in cands if c not in _NAO_CODIGO]


# ── Cupons do carrossel oficial (payload SSR) ────────────────────────────────
# Destinos que não são lista de produtos: publicar isso como cupom manda o usuário
# para uma vitrine sem garantia de que o desconto se aplica. Mesma regra do scraper
# de campanhas (scraper.mapear_cupons).
_URLS_INVALIDAS = ("/social/", "/perfil/", "/usuario/", "/noindex/")
_RE_DINHEIRO = re.compile(r"R\$\s*(\d[\d.]*(?:,\d{2})?)")
_RE_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _valor_reais(texto):
    """'Compra mínima R$ 1.099' -> 1099.0. None quando não há valor no texto."""
    m = _RE_DINHEIRO.search(texto or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def _payload_nordic(html):
    """JSON embutido em `__NORDIC_RENDERING_CTX__`, ou None se a página não o traz.

    Mesmo recorte usado no scraper de campanhas: o script é
    `_n.ctx.r={...};_n.ctx.r.assets.manifest=...`, então o JSON termina no `;`.
    """
    if not isinstance(html, str) or "_n.ctx.r=" not in html:
        return None
    try:
        bruto = html.split("_n.ctx.r=", 1)[1].split(";_n.ctx.r.assets", 1)[0]
        return json.loads(bruto)
    except Exception as e:
        logger.warning("Payload __NORDIC_RENDERING_CTX__ da página de cupons ilegível: %s", e)
        return None


def _cupons_do_payload(dados):
    """Cupons brutos do carrossel, achados pela FORMA do objeto.

    O bloco mora em `brickStack['coupons-carousel-<timestamp>']`, e o timestamp muda
    a cada render — procurar pelo caminho quebraria no dia seguinte. Procuramos por
    dicts que tenham `campaign_id` + `title` + `action`, que é a assinatura estável
    de um cupom, em qualquer lugar da árvore.
    """
    achados, vistos = [], set()

    def visitar(obj):
        if isinstance(obj, list):
            for item in obj:
                visitar(item)
            return
        if not isinstance(obj, dict):
            return
        if obj.get("campaign_id") and isinstance(obj.get("title"), dict) and obj.get("action"):
            camp = str(obj["campaign_id"])
            if camp not in vistos:
                vistos.add(camp)
                achados.append(obj)
            return
        for valor in obj.values():
            visitar(valor)

    visitar(dados)
    return achados


def _normalizar_cupom(bruto):
    """Cupom bruto do payload -> dict pronto para virar CupomNormalizado (ou None).

    Descarta o que não é publicável: sem campanha, expirado/inativo, ou cujo destino
    não é uma lista de produtos.
    """
    camp_id = str(bruto.get("campaign_id") or "").strip()
    if not camp_id:
        return None
    if str((bruto.get("status") or {}).get("id", "")).upper() not in ("", "ACTIVE"):
        return None

    titulo_texto = str((bruto.get("title") or {}).get("text") or "").strip()
    categoria = str(bruto.get("category") or "").strip()
    if not titulo_texto:
        return None

    acao = bruto.get("action") or {}
    destino = str(acao.get("value") or "").strip()
    if not destino.startswith("http"):
        destino = f"https://www.mercadolivre.com.br{destino}" if destino else ""
    # O fragmento `#navigation_id=...` é telemetria de carrossel; atrapalha o
    # casamento por URL e não muda o destino.
    destino = destino.split("#")[0]
    if not destino or any(p in destino for p in _URLS_INVALIDAS):
        return None

    valores = bruto.get("amount") or {}
    tipo = "porcentagem" if str(bruto.get("benefit_mode", "")).upper() == "PERCENT" else "fixo"
    m_pct, m_reais = _RE_PERCENT.search(titulo_texto), _valor_reais(titulo_texto)
    if m_pct:
        tipo, valor = "porcentagem", float(m_pct.group(1).replace(",", "."))
    elif m_reais is not None:
        tipo, valor = "fixo", m_reais
    else:
        valor = None

    segmentacoes = bruto.get("segmentations") or {}
    container = segmentacoes.get("container") or {}
    return {
        "campanha_id": camp_id,
        "titulo": f"{titulo_texto} — {categoria}".strip(" —") if categoria else titulo_texto,
        "categoria": categoria,
        "container_url": destino,
        "container_name": str(container.get("name") or container.get("id") or ""),
        "tipo_desconto": tipo,
        "valor_desconto": valor,
        "valor_minimo": _valor_reais(valores.get("min_amount")),
        "desconto_maximo": _valor_reais(valores.get("cap_amount")),
        "validade": parse_datetime(str(bruto.get("expiration_date") or "")) or None,
        "total_itens": segmentacoes.get("total_items"),
    }


def _salvar_cupons_smart(cupons):
    """Upsert dos cupons do carrossel como CupomNormalizado com container_url."""
    if not cupons:
        return 0
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="mercadolivre-web", defaults={
            "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
    n = 0
    for c in cupons:
        regras = {
            "tipo_desconto": c["tipo_desconto"],
            "valor_desconto": c["valor_desconto"],
            "valor_minimo": c["valor_minimo"],
            "desconto_maximo": c["desconto_maximo"],
            # Cupom de clique, não de digitação: a mensagem tem de dizer "ative",
            # nunca oferecer um código para colar no checkout.
            "modo_resgate": "ativacao",
            "escopo": c["categoria"],
            "container_url": c["container_url"],
            "container_name": c["container_name"],
            # Vale só para os itens do container; nunca é cupom de site inteiro.
            "is_mar_aberto": False,
            "dia_inicio": "", "dia_fim": "",
        }
        CupomNormalizado.objects.update_or_create(
            fonte=fonte, external_id=f"campanha:{c['campanha_id']}",
            defaults={
                "marketplace": "mercadolivre",
                "titulo": c["titulo"][:255],
                # `code` do payload é token opaco de ativação, não código digitável.
                "codigo": "",
                "categoria": c["categoria"] or derivar_categoria_cupom(c["titulo"], regras),
                "anunciante_nome": rotulo_anunciante(
                    c["titulo"], regras, categoria_fallback=c["categoria"]),
                "link": c["container_url"][:1000],
                "validade": c["validade"],
                # Oficial do ML, mas a associação item-a-item só vira 'confirmado'
                # depois que casar_cupons_container abrir a lista.
                "confianca": "media",
                "estado": "ativo",
                "regras": regras,
                "evidencia": {"transport": "public-web", "association": "unverified",
                              "total_items": c["total_itens"]},
            },
        )
        n += 1
    logger.info("Cupons oficiais do carrossel ML: %s upsert(s)", n)
    return n


def _coletar_cupons_smart(page):
    """Lê o payload da página aberta e devolve os cupons normalizados. Nunca levanta."""
    try:
        html = page.content()
    except Exception as e:
        logger.warning("Não foi possível ler o HTML da página de cupons ML: %s", e)
        return []
    dados = _payload_nordic(html)
    if dados is None:
        return []
    return [c for c in (_normalizar_cupom(b) for b in _cupons_do_payload(dados)) if c]


def mapear_cupons_codigo(faixa=None, usuario=None):
    """Raspa /ofertas/cupons: produtos -> origem='cupom_codigo'; códigos -> CupomCodigo.

    `faixa` (ini, fim) liga o progresso na tela; sem ela a linha sai sem % (é o que
    o ciclo automático faz — não há barra para alimentar).

    `usuario` define de quem é a credencial (sem ele, a organização de sistema —
    ver scrapers/ml_auth.py). A página mostra menos cupons deslogada.
    """
    logger.info("Iniciando raspagem de cupons de codigo ML")
    state = storage_state(usuario)
    if state is None:
        avisar_sem_sessao("Raspagem de cupons de checkout", usuario)
    coletados, codigos = [], set()
    paginas_sem_codigo = 0
    smart = []
    links_vistos = set()

    with iniciar_browser(storage_state=state, headless=True,
                         validar_sessao=False) as (page, context):
        for n in range(1, 6):  # algumas páginas
            emitir_fase(f"Cupons de checkout — página {n}/5", n / 5, faixa)
            url = "https://www.mercadolivre.com.br/ofertas/cupons"
            if n > 1:
                url += f"?page={n}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Erro ao carregar pagina %s de cupons de codigo ML: %s", n, e)
                break

            # Saída principal: os cupons de verdade vivem no payload SSR, não no DOM.
            if not smart:
                smart = _coletar_cupons_smart(page)

            cards = _coletar_cards(page)
            if not cards:
                break
            # `?page=N` é IGNORADO por esta página: as páginas 2..5 devolvem os mesmos
            # itens da 1ª. Sem isto a varredura gastava 5 carregamentos para repetir o
            # mesmo conteúdo (o upsert deduplica, mas o tempo é perdido).
            novos = [c for c in cards if c["link_produto"] not in links_vistos]
            if not novos:
                logger.info("Página %s de cupons repetiu os itens da anterior; parando", n)
                break
            links_vistos.update(c["link_produto"] for c in novos)
            coletados.extend(novos)
            try:
                achados = _extrair_codigos(page.locator("body").inner_text(timeout=5000))
                codigos.update(achados)
                if not achados:
                    paginas_sem_codigo += 1
            except Exception as e:
                # Era `except Exception: pass`. Com o inner_text estourando o
                # resultado era zero códigos, zero log — e como `coletados` não
                # estava vazio, a raspagem ainda reportava sucesso. O scraper dizia
                # "ok" sem ter trazido um único cupom.
                paginas_sem_codigo += 1
                logger.warning("Não foi possível ler os códigos da página %s de "
                               "cupons ML: %s", n, e)

    # Os cupons do carrossel são a saída principal e independem dos cards do DOM,
    # então são gravados antes da guarda anti-wipe abaixo.
    n_smart = _salvar_cupons_smart(smart)

    # Guarda anti-wipe: se a raspagem não trouxe NADA (ML bloqueou/caiu), não apaga
    # os produtos nem desativa códigos válidos — evita zerar tudo por falha de rede.
    if not coletados and not codigos and not smart:
        logger.warning("Raspagem de cupons de codigo ML vazia; nada alterado")
        # Sem prefixo [PROGRESSO]: isto é log, tem que ficar na tela depois que a
        # barra passar, não virar legenda efêmera da barra.
        emitir_progresso("Aviso: a página de cupons do ML não devolveu nenhum item "
                         "(nada foi alterado).")
        return 0

    # Produtos do cupom-código são efêmeros (recriados a cada raspagem, como as outras
    # lanes). Só troca se veio conteúdo novo (o guard acima já barra o caso 100% vazio).
    n_prod = 0
    if coletados:
        n_prod = _salvar(coletados, origem="cupom_codigo")

    # Códigos: upsert + REATIVA os vistos agora; DESATIVA os automáticos que sumiram
    # (antes só criava e nunca marcava stale → códigos mortos ficavam na lista global).
    n_novos = 0
    for cod in codigos:
        _, criado = CupomCodigo.objects.update_or_create(
            codigo=cod, defaults={"ativo": True})
        # descricao só na criação (não sobrescreve edição manual)
        if criado:
            CupomCodigo.objects.filter(codigo=cod).update(descricao=_DESC_AUTO)
            n_novos += 1
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
        regras_cod = {"tipo_desconto": "", "valor_desconto": None,
                      "valor_minimo": None, "desconto_maximo": None,
                      "modo_resgate": "codigo", "escopo": "",
                      "container_url": "", "container_name": "",
                      "is_mar_aberto": False, "dia_inicio": "", "dia_fim": ""}
        CupomNormalizado.objects.update_or_create(
            fonte=fonte, external_id=f"checkout:{cod}",
            defaults={"marketplace": "mercadolivre", "titulo": f"Cupom {cod}",
                      "codigo": cod, "link": "https://www.mercadolivre.com.br/ofertas/cupons",
                      "categoria": derivar_categoria_cupom(f"Cupom {cod}", regras_cod),
                      "regras": regras_cod,
                      "confianca": "baixa", "estado": "ativo",
                      "evidencia": {"transport": "public-web", "association": "unverified"}},
        )

    # Uma página pode continuar trazendo produtos e ocultar os banners/códigos por
    # experimento ou localização. Zero códigos não é evidência de expiração.
    n_stale = 0
    if codigos:
        stale = (CupomCodigo.objects
                 .filter(descricao=_DESC_AUTO, ativo=True)
                 .exclude(codigo__in=codigos))
        n_stale = stale.update(ativo=False)

    logger.info(
        "Cupons ML: %s cupons oficiais, %s produtos, %s codigos (%s novos, %s desativados)",
        n_smart, n_prod, len(codigos), n_novos, n_stale,
    )
    # Zero código NÃO é mais anomalia: a página migrou para cupons de ativação por
    # clique e quase não publica mais código digitável. O que merece alerta é sair
    # daqui sem NENHUMA das três saídas — aí a página mudou de novo.
    if not smart and not codigos:
        logger.warning("Raspagem de cupons ML: nenhum cupom oficial e nenhum codigo "
                       "em %s pagina(s) (%s produto(s) coletados)",
                       paginas_sem_codigo, len(coletados))
        emitir_progresso(
            "Aviso: a página de cupons do ML não devolveu nenhum cupom "
            f"({len(coletados)} produto(s) de oferta coletados).")
    return n_prod
