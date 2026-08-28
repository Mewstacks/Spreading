"""Canais públicos do Telegram lidos por HTTP puro — sem userbot e sem credencial.

O worker `monitorar_canais` já lia canais, mas exige Telethon com
`TELEGRAM_API_ID/API_HASH/SESSION`, ou seja, uma conta de Telegram de verdade
pareada e um segredo que equivale a essa conta. Enquanto esses três valores não
existem em produção o worker fica ocioso — e ficou.

Só que todo canal público do Telegram publica uma prévia em `https://t.me/s/<canal>`:
as 20 mensagens mais recentes, em HTML server-side, sem login. É o mesmo endereço que
qualquer pessoa abre no navegador. Isso transforma "descobrir oferta em canal" numa
fonte comum do pipeline: HTTP, sem Chromium, sem segredo, rodando junto das outras.

**O que esta fonte é e o que ela não é.** Ela emite `Produto` candidato a partir do
link que o canal publicou. O preço que aparece na mensagem é *alegação de terceiro* e
entra só como evidência — nunca como preço de referência, porque preço de referência
é o que decide se algo é "ótima promoção". Quem confere de verdade é o caminho normal
de envio, que reabre o destino, confirma que o anúncio está vivo e revalida o preço no
momento da publicação. Uma fonte que se declarasse confiável aqui colocaria a
reputação de quem publica na mão de um canal desconhecido.

Complementar, não substituto: o worker Telethon continua sendo o caminho para
re-divulgar a mensagem original em tempo quase real. Esta fonte serve para o catálogo
— e funciona hoje, sem esperar credencial nenhuma.
"""
import html
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone
from concurrent.futures import ThreadPoolExecutor

import requests
from django.utils import timezone

from apps.scrapers.canais.seeds import CANAIS_SUGERIDOS
from apps.scrapers.coupon_rules import normalizar_regras_cupom
from apps.scrapers.cupom_extractor import extrair, parece_ter_cupom
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro

logger = logging.getLogger(__name__)

BASE = "https://t.me/s/"
_TIMEOUT = (4, 12)
_REDIRECT_TIMEOUT = (3, 7)
_REDIRECT_WORKERS = 16
_CHANNEL_WORKERS = 6
_PAGE_CACHE_TTL_SECONDS = 120
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Handle do Telegram: letras, números e _, de 5 a 32 caracteres. Restringir aqui é o
# que impede um handle vindo do banco de virar caminho arbitrário na URL.
_HANDLE_OK = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_BLOCO_MENSAGEM = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S,
)
_POST_ID = re.compile(r'data-post="([^"]+)"')
_TAG = re.compile(r"<[^>]+>")
_QUEBRA = re.compile(r"<br\s*/?>", re.I)

_URL = re.compile(r"https?://[^\s<>\"']+")
_PRECO = re.compile(r"R\$\s*([\d.]+,\d{2}|\d+,\d{2}|\d+)")
# "Cupom ABC10", "cupom: ABC10", "use o cupom ABC10". Exige 4+ caracteres e ao menos
# um dígito ou 6+ letras, senão qualquer palavra depois de "cupom" virava código.
_CUPOM = re.compile(
    r"cupom[:\s]+([A-Z0-9][A-Z0-9._-]{3,29})\b", re.I,
)

_LOJAS = (
    ("mercadolivre", ("mercadolivre.com", "mercadolibre.com", "meli.la")),
    ("amazon", ("amazon.com.br", "amzn.to", "amzn.eu")),
    ("shopee", ("shopee.com.br", "s.shopee.com.br", "shope.ee")),
)

# Encurtadores: o destino real só se conhece seguindo o redirect.
_ENCURTADORES = ("meli.la", "amzn.to", "amzn.eu", "shope.ee", "s.shopee.com.br")

# Páginas que NÃO são produto. Medido em 18/08/2026: os canais de oferta publicam,
# em massa, links `meli.la` que caem em `/social/<perfil>` — a vitrine de afiliado
# de quem postou, não um anúncio. Numa amostra de 12 links de dois canais grandes,
# ZERO era página de produto. Sem este filtro a fonte enchia o catálogo de linhas
# que o Programa de Afiliados recusa (`link._montar_url_isca` já barra `/social/`)
# e que nunca virariam envio — falso positivo em volume industrial.
_NAO_E_PRODUTO = ("/social/", "/perfil/", "/usuario/", "/noindex/", "/pagina/",
                  "/lista/", "/ofertas", "/promocoes")

# Marcas de página de produto por loja.
_E_PRODUTO = {
    "mercadolivre": ("/mlb-", "/p/mlb", "produto.mercadolivre", "item_id=mlb"),
    "amazon": ("/dp/", "/gp/product/"),
    "shopee": ("-i.", "/product/"),
}


def _marketplace(url: str) -> str:
    texto = str(url or "").lower()
    for slug, dominios in _LOJAS:
        if any(d in texto for d in dominios):
            return slug
    return ""


def _texto_limpo(bruto: str) -> str:
    return html.unescape(_TAG.sub("", _QUEBRA.sub("\n", bruto))).strip()


def e_pagina_de_produto(url: str, slug: str) -> bool:
    """A URL aponta para um ANÚNCIO, não para uma vitrine?

    Este é o portão que separa oferta de ruído. Vitrine de afiliado, perfil e
    listagem não têm preço, não têm estoque e o Programa de Afiliados nem aceita —
    publicar uma delas é mandar o grupo para uma página que não é a oferta prometida.
    """
    texto = str(url or "").lower()
    if any(marca in texto for marca in _NAO_E_PRODUTO):
        return False
    return any(marca in texto for marca in _E_PRODUTO.get(slug, ()))


def resolver(url: str, sessao=None) -> str:
    """Segue o redirect de um encurtador e devolve a URL final.

    Encurtador é opaco por definição: `meli.la/2QxaZLw` pode ser um anúncio ou a
    vitrine de quem postou, e só o destino conta. Falha de rede devolve string vazia,
    que o chamador trata como "não sei" e descarta — melhor perder uma oferta do que
    publicar uma que não se pôde conferir.
    """
    cliente = sessao or requests
    try:
        resposta = cliente.get(
            url, timeout=_REDIRECT_TIMEOUT, headers={"User-Agent": _UA},
            allow_redirects=True, stream=True,
        )
        final = str(resposta.url or "")
        resposta.close()
        return final
    except requests.RequestException:
        return ""


_PERCENTUAL_CITADO = re.compile(r"(\d{1,2})\s*%")


def _percentual_citado(texto: str) -> float:
    """Percentual escrito na mensagem. 0 quando não há número confiável.

    100% não existe em cupom de varejo — é erro de leitura ou promessa falsa —, e
    valor sem número nenhum deixa o cupom sem o que anunciar.
    """
    achado = _PERCENTUAL_CITADO.search(texto or "")
    if not achado:
        return 0.0
    valor = int(achado.group(1))
    return float(valor) if 0 < valor < 100 else 0.0


# Palavras que aparecem logo depois de "cupom" e NÃO são código. Medido: a regex
# lia "cupom Mercado Livre" e emitia o código `MERCADO`, que ninguém digita em
# checkout nenhum. Um código inventado é pior que nenhum — ele vai para o grupo com
# a assinatura do influenciador e não funciona.
_NAO_E_CODIGO = {
    "MERCADO", "LIVRE", "AMAZON", "SHOPEE", "MAGALU", "AMERICANAS", "CUPOM",
    "CUPONS", "DESCONTO", "DESCONTOS", "OFERTA", "OFERTAS", "PROMO", "PROMOCAO",
    "PROMOCOES", "EXCLUSIVO", "VALIDO", "APENAS", "SOMENTE", "PRIMEIRA",
    "COMPRA", "FRETE", "GRATIS", "LIMITADO", "ATIVE", "CLIQUE", "LINK", "AQUI",
    "HOJE", "AGORA", "NOVO", "NOVOS", "MELHOR", "MELHORES", "SELECIONADOS",
}


def _codigo_cupom(texto: str) -> str:
    achado = _CUPOM.search(texto or "")
    if not achado:
        return ""
    codigo = achado.group(1).strip().upper()
    if codigo in _NAO_E_CODIGO:
        return ""
    # Só letras exige tamanho: palavra curta depois de "cupom" é quase sempre
    # continuação da frase, não código.
    if codigo.isalpha() and len(codigo) < 8:
        return ""
    return codigo


class TelegramPublicoSource(SourceAdapter):
    slug = "telegram-publico"
    marketplace = "multiloja"
    name = "Telegram — canais públicos (prévia web)"
    requires_chromium = False
    # Vitrine curada / prévia recente: recorte por construção, nunca inventário.
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"
        self._page_cache = {}

    def _canais(self, handles=None):
        if handles:
            return [str(h).strip().lstrip("@") for h in handles if str(h).strip()]
        return [c["handle"] for c in CANAIS_SUGERIDOS]

    def _baixar(self, handle):
        if not _HANDLE_OK.match(handle):
            logger.warning("Handle de canal recusado: %r", handle[:40])
            return ""
        cached = self._page_cache.get(handle)
        now = time.monotonic()
        if cached and now - cached[0] < _PAGE_CACHE_TTL_SECONDS:
            return cached[1]
        resposta = requests.get(
            f"{BASE}{handle}", timeout=_TIMEOUT, headers={"User-Agent": _UA},
        )
        corpo = resposta.text or "" if resposta.status_code == 200 else ""
        self._page_cache[handle] = (now, corpo)
        return corpo

    def _carregar_canais(self, handles):
        """Baixa previews em paralelo, isolando timeout/falha por canal."""
        alvos = list(handles)[:12]

        def carregar(handle):
            try:
                return handle, self._baixar(handle), ""
            except requests.RequestException as exc:
                logger.info(
                    "Canal @%s indisponível (%s).", handle, type(exc).__name__,
                )
                return handle, "", type(exc).__name__

        with ThreadPoolExecutor(
            max_workers=min(_CHANNEL_WORKERS, max(1, len(alvos))),
        ) as executor:
            return list(executor.map(carregar, alvos))

    def _mensagens(self, corpo):
        """(post_id, texto) das mensagens da prévia, na ordem em que aparecem."""
        ids = _POST_ID.findall(corpo)
        blocos = _BLOCO_MENSAGEM.findall(corpo)
        # As duas listas costumam ter o mesmo tamanho; quando não têm, o id é opcional
        # e o texto é o que importa — melhor perder o id do que perder a mensagem.
        for indice, bloco in enumerate(blocos):
            post = ids[indice] if indice < len(ids) else ""
            yield post, _texto_limpo(bloco)

    def discover_offers(self, canais=None, include_offers=True, **kwargs):
        if not include_offers:
            self.last_metrics = {"offers_skipped": True, "complete": False}
            return
        handles = self._canais(canais)
        agora = timezone.now()
        vistos = set()
        lidos = falhas = 0
        # Contadores de descarte: sem eles, uma fonte que rejeita tudo fica idêntica
        # a uma fonte sem novidade. A diferença é o que diz se o parser quebrou.
        descartados = {"nao_e_produto": 0, "nao_resolveu": 0}

        mensagens = []
        for handle, corpo, _erro in self._carregar_canais(handles):
            if not corpo:
                falhas += 1
                continue
            lidos += 1
            for post, texto in self._mensagens(corpo):
                if not texto:
                    continue
                mensagens.append((handle, post, texto))

        encurtados = []
        for _handle, _post, texto in mensagens:
            for bruto in _URL.findall(texto):
                bruto = bruto.rstrip(").,;")
                if any(d in bruto.lower() for d in _ENCURTADORES):
                    encurtados.append(bruto)
        unicos = list(dict.fromkeys(encurtados))
        with ThreadPoolExecutor(max_workers=_REDIRECT_WORKERS) as executor:
            destinos = dict(zip(unicos, executor.map(resolver, unicos)))

        for handle, post, texto in mensagens:
                preco_alegado = 0.0
                achado_preco = _PRECO.search(texto)
                if achado_preco:
                    preco_alegado = normalizar_dinheiro(achado_preco.group(1))
                codigo = _codigo_cupom(texto)
                for bruto in _URL.findall(texto):
                    bruto = bruto.rstrip(").,;")
                    slug = _marketplace(bruto)
                    if not slug:
                        continue
                    # Encurtador é opaco: resolve antes de julgar. Sem isto, todo
                    # `meli.la` entrava como se fosse anúncio.
                    url = bruto
                    if any(d in bruto.lower() for d in _ENCURTADORES):
                        url = destinos.get(bruto, "")
                        if not url:
                            descartados["nao_resolveu"] += 1
                            continue
                        slug = _marketplace(url) or slug
                    if not e_pagina_de_produto(url, slug):
                        descartados["nao_e_produto"] += 1
                        continue
                    chave = f"tg:{slug}:{url}"
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    titulo = next(
                        (linha.strip() for linha in texto.splitlines() if linha.strip()),
                        "Oferta de canal",
                    )
                    yield IngestedItem(
                        external_id=chave[:160], marketplace=slug, source=self.slug,
                        kind="offer", canonical_url=url[:1000], title=titulo[:255],
                        # `current_price` fica ZERADO de propósito: o preço da
                        # mensagem é alegação e não pode alimentar o cálculo de
                        # desconto. Ele viaja em `evidence` para diagnóstico e o
                        # preço real vem da revalidação no envio.
                        current_price=0.0, reference_price=0.0,
                        observed_at=agora,
                        evidence={
                            "transport": "telegram-preview",
                            "canal": handle,
                            "post": post,
                            "preco_alegado": preco_alegado,
                            "cupom_citado": codigo,
                            "trecho": texto[:300],
                        },
                    )
        self.last_health_status = "healthy" if lidos else "degraded"
        self.last_metrics = {
            "canais_lidos": lidos,
            "canais_falhos": falhas,
            "itens": len(vistos),
            "descartados": dict(descartados),
            "redirects_total": len(unicos),
            "redirects_resolvidos": sum(bool(url) for url in destinos.values()),
            # Nunca "completo": a prévia mostra só as mensagens recentes, então
            # ausência aqui não prova que a oferta sumiu e não pode expirar catálogo.
            "complete": False,
        }

    def discover_coupons(self, canais=None, **kwargs):
        """Códigos citados nas mensagens — o que estes canais realmente carregam.

        A medição de 19/08/2026 foi clara: dos 78 links publicados por seis canais,
        NENHUM era página de produto (todos caem na vitrine de afiliado de quem
        postou), mas 26 mensagens citavam um código de cupom no texto. O valor
        destes canais não está no link, está no código.

        Código visto aqui é ALEGAÇÃO e entra como tal, com a precedência mais fraca
        de todas. Ele vale por corroborar o que uma fonte oficial já viu e por
        apontar código que nos escapou — e a prova de que o mecanismo funciona já
        existe: os 10 cupons de ML do Promobit bateram 10/10 com a página oficial de
        afiliados, mesmo código e mesmo percentual.

        Sem percentual no texto o cupom não é emitido: `_preflight` o descartaria
        adiante por `missing_discount`, e sujar o funil com código sem valor só
        atrapalha quem lê o diagnóstico.
        """
        handles = self._canais(canais)
        agora = timezone.now()
        vistos = set()
        lidos = falhas = sem_valor = 0

        for handle, corpo, _erro in self._carregar_canais(handles):
            if not corpo:
                falhas += 1
                continue
            lidos += 1
            for post, texto in self._mensagens(corpo):
                if not parece_ter_cupom(texto):
                    continue
                # A loja do link acompanha a leitura como dica: quando a mensagem não
                # nomeia a loja, o domínio do link resolve. Código anunciado na loja
                # errada é o cupom que "não funciona" na mão de quem publicou.
                loja_do_link = ""
                for bruto in _URL.findall(texto):
                    loja_do_link = _marketplace(bruto)
                    if loja_do_link:
                        break
                achados = extrair(texto, loja_padrao=loja_do_link)
                if not achados:
                    sem_valor += 1
                    continue
                for cupom in achados:
                    chave = f"telegram:{cupom['loja']}:{cupom['codigo']}"
                    if chave in vistos:
                        continue
                    vistos.add(chave)
                    regras = normalizar_regras_cupom({
                        "tipo_desconto": cupom["tipo"],
                        "valor_desconto": cupom["valor"],
                        "valor_minimo": cupom["minimo"],
                        "modo_resgate": "codigo",
                        "escopo": cupom["escopo"],
                    }, external_id=chave, codigo=cupom["codigo"])
                    yield IngestedItem(
                        external_id=chave[:160], marketplace=cupom["loja"],
                        source=self.slug, kind="coupon", canonical_url="",
                        title=f"Cupom {cupom['codigo']}"[:255],
                        coupon_code=cupom["codigo"][:120], coupon_rules=regras,
                        content_type="voucher", observed_at=agora,
                        evidence={
                            "transport": "telegram-preview-llm",
                            "canal": handle,
                            "post": post,
                            "confianca_origem": "comunidade",
                            "teto_desconto": cupom["teto"],
                            "trecho": texto[:300],
                        },
                    )
        self.last_health_status = "healthy" if lidos else "degraded"
        self.last_metrics = {
            **self.last_metrics,
            "canais_lidos": lidos,
            "canais_falhos": falhas,
            "cupons": len(vistos),
            "sem_cupom_legivel": sem_valor,
            # Nunca completo: a prévia mostra só as mensagens recentes.
            "complete": False,
        }

    def healthcheck(self):
        return {
            "ok": self.last_health_status == "healthy",
            "status": self.last_health_status,
            "metrics": self.last_metrics,
        }
