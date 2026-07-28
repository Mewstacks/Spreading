"""Revalidação de preço imediatamente antes de publicar.

O catálogo é montado por raspagens periódicas; `expire_stale` só marca um item
como velho depois de 48h. Sem esta checagem a mensagem podia anunciar um preço
de dois dias atrás — foi a divergência observada entre a promoção enviada e a
página da Amazon.

Política (decidida com o time):
  - variação dentro da tolerância  -> segue, nada é gravado;
  - preço caiu                     -> atualiza e segue (anunciar mais caro do que
                                      a página cobra nunca gera reclamação);
  - preço subiu                    -> atualiza e só aborta quando o desconto cai
                                      abaixo do mínimo configurado;
  - erro/timeout                   -> inconclusivo, segue (instabilidade de API
                                      não pode bloquear o envio).
"""
import logging
import time

from django.conf import settings
from django.utils import timezone

from apps.scrapers import precos

logger = logging.getLogger(__name__)

# Abaixo disso a diferença é arredondamento de centavo, não mudança de preço.
TOLERANCIA = 0.005


def _resultado(ok, preco, *, mudou=False, fonte="", motivo=""):
    return {"ok": ok, "preco": preco, "mudou": mudou, "fonte": fonte, "motivo": motivo}


def _preco_da_creators_api(produto):
    """Preço oficial via Creators API — uma chamada HTTP, sem Playwright."""
    from apps.scrapers.scraper_amazon import creators_api
    from apps.scrapers.scraper_amazon.ofertas_scraper import _mapear_item

    asin = getattr(produto, "asin", "")
    if not asin:
        return None
    creds = creators_api.creds_de_usuario(getattr(produto, "owner", None))
    itens = creators_api.get_items([asin], creds=creds)
    if not itens:
        return None
    mapeado = _mapear_item(itens[0])
    if not mapeado or mapeado.get("preco_com_cupom", 0) <= 0:
        return None
    return {
        "preco": mapeado["preco_com_cupom"],
        "preco_de": mapeado["preco_sem_desconto"],
        "fonte": "creators-api",
    }


def _preco_da_pdp(produto):
    """Raspagem da PDP. Cara (Playwright) — só atrás da flag."""
    from apps.scrapers.sources.amazon_public import verify_product_url

    resultado = verify_product_url(produto.link_produto)
    preco = (resultado or {}).get("preco") or 0
    if preco <= 0:
        return None
    return {"preco": preco, "preco_de": 0, "fonte": "pdp-publica"}


def _desconto(preco_de, preco):
    if not preco_de or preco_de <= preco:
        return 0.0
    return (preco_de - preco) / preco_de * 100


def revalidar(produto, usuario=None, configuracao=None) -> dict:
    """Confere o preço ao vivo e atualiza o produto. Ver política no topo."""
    if getattr(produto, "marketplace", "") != "amazon":
        return _resultado(True, 0, fonte="nao_suportado")

    atual = getattr(produto, "preco_com_cupom", 0) or 0
    if atual <= 0:
        return _resultado(True, atual, fonte="sem_preco_base")

    inicio = time.monotonic()
    vivo = None
    try:
        vivo = _preco_da_creators_api(produto)
        if vivo is None and getattr(settings, "PRECO_REVALIDA_PLAYWRIGHT", False):
            vivo = _preco_da_pdp(produto)
    except Exception as exc:
        logger.warning(
            "preco_ao_vivo inconclusivo asin=%s: %s", getattr(produto, "asin", ""), exc,
        )
        return _resultado(True, atual, fonte="inconclusivo", motivo=str(exc)[:120])

    decorrido_ms = (time.monotonic() - inicio) * 1000
    if vivo is None:
        return _resultado(True, atual, fonte="inconclusivo", motivo="sem dado ao vivo")

    novo = vivo["preco"]
    variacao = abs(novo - atual) / atual
    logger.info(
        "preco_ao_vivo asin=%s fonte=%s banco=%.2f vivo=%.2f variacao=%.4f ms=%.0f",
        getattr(produto, "asin", ""), vivo["fonte"], atual, novo, variacao, decorrido_ms,
    )
    if variacao <= TOLERANCIA:
        return _resultado(True, atual, fonte=vivo["fonte"])

    preco_de = vivo.get("preco_de") or getattr(produto, "preco_sem_desconto", 0) or 0
    _aplicar(produto, novo, preco_de)

    if novo < atual:
        return _resultado(True, novo, mudou=True, fonte=vivo["fonte"],
                          motivo="preço caiu")

    minimo = _minimo_desconto(usuario, configuracao)
    desconto = _desconto(preco_de, novo)
    if minimo and desconto < minimo:
        return _resultado(
            False, novo, mudou=True, fonte=vivo["fonte"],
            motivo=f"desconto caiu para {desconto:.0f}% (mínimo {minimo:.0f}%)",
        )
    return _resultado(True, novo, mudou=True, fonte=vivo["fonte"], motivo="preço subiu")


def _aplicar(produto, novo, preco_de):
    """Grava o preço fresco e invalida o texto da IA quando ele cita o antigo."""
    campos = ["preco_com_cupom", "preco_efetivo", "ultima_verificacao"]
    produto.preco_com_cupom = novo
    produto.preco_efetivo = novo
    produto.ultima_verificacao = timezone.now()
    if preco_de and preco_de > novo:
        produto.preco_sem_desconto = preco_de
        campos.append("preco_sem_desconto")
    # frase_llm/nome_llm são cache do texto gerado, e o título cita o preço.
    # Mantê-los faria a IA anunciar um valor que a linha "POR" já não mostra.
    if getattr(produto, "frase_llm", ""):
        produto.frase_llm = ""
        campos.append("frase_llm")
    if getattr(produto, "nome_llm", ""):
        produto.nome_llm = ""
        campos.append("nome_llm")
    try:
        produto.save(update_fields=campos)
    except Exception:
        logger.warning("preco_ao_vivo não gravou asin=%s",
                       getattr(produto, "asin", ""), exc_info=True)
    precos.registrar("amazon", getattr(produto, "asin", ""),
                     getattr(produto, "link_produto", ""), novo)


def _minimo_desconto(usuario, configuracao):
    valor = getattr(configuracao, "min_desconto_percent", None)
    try:
        return float(valor) if valor else 0.0
    except (TypeError, ValueError):
        return 0.0
