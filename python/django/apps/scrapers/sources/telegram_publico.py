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
import logging
import re
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests
from django.utils import timezone

from apps.scrapers.canais.seeds import CANAIS_SUGERIDOS
from apps.scrapers.coupon_rules import normalizar_regras_cupom
from apps.scrapers.cupom_extractor import codigo_plausivel, extrair, parece_ter_cupom
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro

logger = logging.getLogger(__name__)

BASE = "https://t.me/s/"
_TIMEOUT = (4, 12)
_REDIRECT_TIMEOUT = (3, 7)
_REDIRECT_WORKERS = 16
_CHANNEL_WORKERS = 6
_PAGE_CACHE_TTL_SECONDS = 120
_REDIRECT_CACHE_TTL_SECONDS = 3600
# A previa continua exibindo as ultimas mensagens mesmo quando o canal esta
# parado. Baixar esse mesmo HTML outra vez nao e uma nova observacao.
_MAX_MESSAGE_AGE = timedelta(hours=48)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

# Handle do Telegram: letras, números e _, de 5 a 32 caracteres. Restringir aqui é o
# que impede um handle vindo do banco de virar caminho arbitrário na URL.
_HANDLE_OK = re.compile(r"^[A-Za-z0-9_]{5,32}$")

_URL = re.compile(r"https?://[^\s<>\"']+")
_PRECO = re.compile(r"R\$\s*([\d.]+,\d{2}|\d+,\d{2}|\d+)")
# "Cupom ABC10", "cupom: ABC10", "use o cupom ABC10". Exige 4+ caracteres e ao menos
# um dígito ou 6+ letras, senão qualquer palavra depois de "cupom" virava código.
_CUPOM = re.compile(
    r"cupom[:\s]+([A-Z0-9][A-Z0-9._-]{3,29})\b", re.I,
)


class _TelegramPreviewParser(HTMLParser):
    """Associa texto, links e horario dentro do mesmo bloco de mensagem.

    Listas de regex independentes se deslocam quando ha um post somente com
    midia. O parser acompanha a arvore e impede que um cupom receba a data do post
    seguinte.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.messages = []
        self._current = None
        self._div_depth = 0
        self._text_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get("class") or "").split())
        if (
            self._current is None and tag == "div"
            and "tgme_widget_message" in classes
            and attrs.get("data-post")
        ):
            self._current = {
                "post": str(attrs["data-post"]), "datetime": "", "parts": [],
            }
            self._div_depth = 1
            self._text_depth = None
            return
        if self._current is None:
            return
        if tag == "div":
            self._div_depth += 1
            if "tgme_widget_message_text" in classes:
                self._text_depth = self._div_depth
        if tag == "time" and attrs.get("datetime"):
            self._current["datetime"] = str(attrs["datetime"])
        if self._text_depth is not None:
            if tag == "br":
                self._current["parts"].append("\n")
            elif tag == "a" and str(attrs.get("href") or "").startswith(("http://", "https://")):
                self._current["parts"].append(f" {attrs['href']} ")

    def handle_endtag(self, tag):
        if self._current is None or tag != "div":
            return
        if self._text_depth == self._div_depth:
            self._text_depth = None
        if self._div_depth == 1:
            text = "".join(self._current["parts"])
            self.messages.append((
                self._current["post"], text.strip(), self._current["datetime"],
            ))
            self._current = None
            self._div_depth = 0
            self._text_depth = None
            return
        self._div_depth -= 1

    def handle_data(self, data):
        if self._current is not None and self._text_depth is not None:
            self._current["parts"].append(data)

_LOJAS = (
    ("mercadolivre", (
        "mercadolivre.com.br", "mercadolivre.com", "mercadolibre.com", "meli.la",
    )),
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

_AMAZON_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)
_ML_ITEM = re.compile(r"(?<![A-Z0-9])MLB[-_ ]?(\d{5,})(?!\d)", re.I)
_SHOPEE_ITEM = (
    re.compile(r"/product/(\d+)/(\d+)(?:[/?#]|$)", re.I),
    re.compile(r"-i\.(\d+)\.(\d+)(?:[/?#]|$)", re.I),
)


def _host_em(host: str, dominio: str) -> bool:
    host = str(host or "").casefold().rstrip(".")
    dominio = str(dominio or "").casefold().rstrip(".")
    return bool(host and (host == dominio or host.endswith(f".{dominio}")))


def _marketplace(url: str) -> str:
    try:
        host = (urlsplit(str(url or "")).hostname or "").casefold()
    except ValueError:
        return ""
    for slug, dominios in _LOJAS:
        if any(_host_em(host, dominio) for dominio in dominios):
            return slug
    return ""


def _produto_canonico(url: str, slug: str):
    """Devolve URL sem tracking e ids apenas para PDP reconhecida da loja."""
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return "", []
    if parsed.scheme.casefold() != "https" or _marketplace(url) != slug:
        return "", []
    path = parsed.path or "/"
    if slug == "amazon":
        match = _AMAZON_ASIN.search(path)
        if not match:
            return "", []
        asin = match.group(1).upper()
        return f"https://www.amazon.com.br/dp/{asin}", [asin]
    if slug == "mercadolivre":
        match = _ML_ITEM.search(path)
        if not match:
            return "", []
        item_id = f"MLB{match.group(1)}"
        return f"https://produto.mercadolivre.com.br/MLB-{match.group(1)}", [item_id]
    if slug == "shopee":
        match = next(
            (found for pattern in _SHOPEE_ITEM if (found := pattern.search(path))),
            None,
        )
        if not match:
            return "", []
        shop_id, item_id = match.groups()
        return (
            f"https://shopee.com.br/product/{shop_id}/{item_id}",
            [item_id, f"{shop_id}_{item_id}"],
        )
    return "", []


def _produtos_da_mensagem(texto: str, destinos=None):
    """Produtos citados na mesma mensagem, agrupados por marketplace."""
    destinos = destinos or {}
    encontrados = {}
    for bruto in _URL.findall(texto or ""):
        bruto = bruto.rstrip(").,;")
        slug = _marketplace(bruto)
        if not slug:
            continue
        destino = bruto
        try:
            host = urlsplit(bruto).hostname
        except ValueError:
            host = ""
        if any(_host_em(host, dominio) for dominio in _ENCURTADORES):
            destino = destinos.get(bruto, "")
            slug = _marketplace(destino) or slug
        canonical, ids = _produto_canonico(destino, slug)
        if not canonical or not ids:
            continue
        bucket = encontrados.setdefault(slug, {"urls": set(), "ids": set()})
        bucket["urls"].add(canonical)
        bucket["ids"].update(ids)
    return encontrados


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
        self._redirect_cache = {}
        self._last_redirect_cache_hits = 0
        self._timestamp_metrics = {}

    def _reset_timestamp_metrics(self):
        self._timestamp_metrics = {
            "mensagens_com_data": 0,
            "mensagens_sem_data": 0,
            "mensagens_antigas_descartadas": 0,
            "mensagens_futuras_descartadas": 0,
        }

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
        # Fontes curadas: a passada completa fica abaixo do orçamento do ciclo com
        # seis downloads paralelos. O teto continua explícito para uma
        # lista configurada no banco não transformar um radar barato em crawler sem
        # limite.
        alvos = list(handles)[:32]

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

    def _resolver_lote(self, urls):
        """Resolve cada encurtador no máximo uma vez por hora por worker."""
        now = time.monotonic()
        result = {}
        missing = []
        for url in urls:
            cached = self._redirect_cache.get(url)
            if cached and now - cached[0] < _REDIRECT_CACHE_TTL_SECONDS:
                result[url] = cached[1]
            else:
                missing.append(url)
        self._last_redirect_cache_hits = len(urls) - len(missing)
        if missing:
            with ThreadPoolExecutor(max_workers=_REDIRECT_WORKERS) as executor:
                resolved = dict(zip(missing, executor.map(resolver, missing)))
            for url, destination in resolved.items():
                self._redirect_cache[url] = (now, destination)
                result[url] = destination
        return result

    def _mensagens(self, corpo, *, agora=None):
        """(post_id, texto, data) atuais, na ordem em que aparecem na prévia."""
        agora = agora or timezone.now()
        parser = _TelegramPreviewParser()
        parser.feed(corpo or "")
        # Data ausente ou inválida falha fechada: um post antigo não pode parecer
        # uma reobservação atual e manter um cupom vencido artificialmente vivo.
        for post, texto, data_bruta in parser.messages:
            if not data_bruta:
                self._timestamp_metrics["mensagens_sem_data"] += 1
                continue
            try:
                publicada_em = datetime.fromisoformat(
                    data_bruta.replace("Z", "+00:00"),
                )
                if publicada_em.tzinfo is None:
                    publicada_em = publicada_em.replace(tzinfo=dt_timezone.utc)
                publicada_em = publicada_em.astimezone(dt_timezone.utc)
            except (TypeError, ValueError):
                self._timestamp_metrics["mensagens_sem_data"] += 1
                continue
            self._timestamp_metrics["mensagens_com_data"] += 1
            if publicada_em > agora + _MAX_FUTURE_SKEW:
                self._timestamp_metrics["mensagens_futuras_descartadas"] += 1
                continue
            if publicada_em < agora - _MAX_MESSAGE_AGE:
                self._timestamp_metrics["mensagens_antigas_descartadas"] += 1
                continue
            yield post, texto, publicada_em

    @staticmethod
    def _partes_mensagem(linha, agora):
        """Aceita pares legados de adaptadores/testes e triplas datadas."""
        post, texto, *resto = linha
        return post, texto, (resto[0] if resto else agora)

    def discover_offers(self, canais=None, include_offers=True, **kwargs):
        if not include_offers:
            self.last_metrics = {"offers_skipped": True, "complete": False}
            return
        handles = self._canais(canais)
        agora = timezone.now()
        self._reset_timestamp_metrics()
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
            for linha in self._mensagens(corpo, agora=agora):
                post, texto, publicada_em = self._partes_mensagem(linha, agora)
                if not texto:
                    continue
                mensagens.append((handle, post, texto, publicada_em))

        encurtados = []
        for _handle, _post, texto, _publicada_em in mensagens:
            for bruto in _URL.findall(texto):
                bruto = bruto.rstrip(").,;")
                if any(d in bruto.lower() for d in _ENCURTADORES):
                    encurtados.append(bruto)
        unicos = list(dict.fromkeys(encurtados))
        destinos = self._resolver_lote(unicos)

        for handle, post, texto, publicada_em in mensagens:
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
                        observed_at=publicada_em,
                        evidence={
                            "transport": "telegram-preview",
                            "canal": handle,
                            "post": post,
                            "preco_alegado": preco_alegado,
                            "cupom_citado": codigo,
                            "trecho": texto[:300],
                        },
                    )
        timestamps_ok = (
            self._timestamp_metrics["mensagens_com_data"] > 0
            or self._timestamp_metrics["mensagens_sem_data"] == 0
        )
        self.last_health_status = "healthy" if lidos and timestamps_ok else "degraded"
        self.last_metrics = {
            "canais_lidos": lidos,
            "canais_falhos": falhas,
            "itens": len(vistos),
            "descartados": dict(descartados),
            "redirects_total": len(unicos),
            "redirects_resolvidos": sum(bool(url) for url in destinos.values()),
            "redirects_cache_hits": self._last_redirect_cache_hits,
            **self._timestamp_metrics,
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
        handle_codes = {
            re.sub(r"[^A-Z0-9]", "", handle.upper()) for handle in handles
        }
        agora = timezone.now()
        self._reset_timestamp_metrics()
        candidatos = {}
        lidos = falhas = sem_valor = codigos_handle = 0
        mensagens = []
        for handle, corpo, _erro in self._carregar_canais(handles):
            if not corpo:
                falhas += 1
                continue
            lidos += 1
            for linha in self._mensagens(corpo, agora=agora):
                post, texto, publicada_em = self._partes_mensagem(linha, agora)
                if not parece_ter_cupom(texto):
                    continue
                mensagens.append((handle, post, texto, publicada_em))

        mensagens_validas = []
        for handle, post, texto, publicada_em in mensagens:
            loja_do_link = ""
            for bruto in _URL.findall(texto):
                loja_do_link = _marketplace(bruto)
                if loja_do_link:
                    break
            achados = extrair(texto, loja_padrao=loja_do_link)
            validos = []
            for cupom in achados:
                normalized_code = re.sub(
                    r"[^A-Z0-9]", "", str(cupom.get("codigo") or "").upper(),
                )
                if (not codigo_plausivel(cupom.get("codigo"))
                        or normalized_code in handle_codes):
                    codigos_handle += 1
                    continue
                validos.append(cupom)
            if validos:
                mensagens_validas.append(
                    (handle, post, texto, publicada_em, validos)
                )
            else:
                sem_valor += 1

        encurtados = []
        for _handle, _post, texto, _publicada_em, _cupons in mensagens_validas:
            for bruto in _URL.findall(texto):
                bruto = bruto.rstrip(").,;")
                try:
                    host = urlsplit(bruto).hostname
                except ValueError:
                    host = ""
                if any(_host_em(host, dominio) for dominio in _ENCURTADORES):
                    encurtados.append(bruto)
        unicos = list(dict.fromkeys(encurtados))
        destinos = self._resolver_lote(unicos)

        for handle, post, texto, publicada_em, achados in mensagens_validas:
                produtos = _produtos_da_mensagem(texto, destinos)
                for cupom in achados:
                    chave = f"telegram:{cupom['loja']}:{cupom['codigo']}"
                    registro = candidatos.get(chave)
                    if registro is None:
                        regras = normalizar_regras_cupom({
                            "tipo_desconto": cupom["tipo"],
                            "valor_desconto": cupom["valor"],
                            "valor_minimo": cupom["minimo"],
                            "modo_resgate": "codigo",
                            "escopo": cupom["escopo"],
                        }, external_id=chave, codigo=cupom["codigo"])
                        registro = candidatos[chave] = {
                            "cupom": cupom, "regras": regras, "urls": set(),
                            "ids": set(), "canais": set(), "posts": [],
                            "trecho": texto[:300], "observed_at": publicada_em,
                        }
                    elif publicada_em > registro["observed_at"]:
                        registro["observed_at"] = publicada_em
                    referencia = produtos.get(cupom["loja"], {})
                    registro["urls"].update(referencia.get("urls") or set())
                    registro["ids"].update(referencia.get("ids") or set())
                    registro["canais"].add(handle)
                    if post and len(registro["posts"]) < 5:
                        registro["posts"].append(post)

        for chave, registro in candidatos.items():
            cupom = registro["cupom"]
            urls = sorted(registro["urls"])
            ids = sorted(registro["ids"])
            evidence = {
                "transport": "telegram-preview-parser",
                "canal": sorted(registro["canais"])[0],
                "canais": sorted(registro["canais"])[:5],
                "posts": registro["posts"],
                "confianca_origem": "comunidade",
                "teto_desconto": cupom["teto"],
                "trecho": registro["trecho"],
            }
            if ids:
                evidence.update({
                    "association": "same_public_telegram_message",
                    "product_ids": ids,
                })
                if cupom["loja"] == "amazon":
                    evidence["asins"] = ids
                else:
                    evidence["item_ids"] = ids
            yield IngestedItem(
                external_id=chave[:160], marketplace=cupom["loja"],
                source=self.slug, kind="coupon",
                canonical_url=(urls[0] if urls else ""),
                title=f"Cupom {cupom['codigo']}"[:255],
                coupon_code=cupom["codigo"][:120],
                coupon_rules=registro["regras"], content_type="voucher",
                observed_at=registro["observed_at"], evidence=evidence,
            )
        timestamps_ok = (
            self._timestamp_metrics["mensagens_com_data"] > 0
            or self._timestamp_metrics["mensagens_sem_data"] == 0
        )
        self.last_health_status = "healthy" if lidos and timestamps_ok else "degraded"
        self.last_metrics = {
            **self.last_metrics,
            "canais_lidos": lidos,
            "canais_falhos": falhas,
            "cupons": len(candidatos),
            "cupons_com_produto": sum(bool(row["ids"]) for row in candidatos.values()),
            "redirects_cupom_total": len(unicos),
            "redirects_cupom_resolvidos": sum(bool(url) for url in destinos.values()),
            "redirects_cupom_cache_hits": self._last_redirect_cache_hits,
            "sem_cupom_legivel": sem_valor,
            "codigos_ruidosos_descartados": codigos_handle,
            **self._timestamp_metrics,
            # Nunca completo: a prévia mostra só as mensagens recentes.
            "complete": False,
        }

    def healthcheck(self):
        return {
            "ok": self.last_health_status == "healthy",
            "status": self.last_health_status,
            "metrics": self.last_metrics,
        }
