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
from datetime import datetime, timezone as dt_timezone

import requests
from django.utils import timezone

from apps.scrapers.canais.seeds import CANAIS_SUGERIDOS
from .base import IngestedItem, SourceAdapter, normalizar_dinheiro

logger = logging.getLogger(__name__)

BASE = "https://t.me/s/"
_TIMEOUT = (5, 20)
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


def _marketplace(url: str) -> str:
    texto = str(url or "").lower()
    for slug, dominios in _LOJAS:
        if any(d in texto for d in dominios):
            return slug
    return ""


def _texto_limpo(bruto: str) -> str:
    return html.unescape(_TAG.sub("", _QUEBRA.sub("\n", bruto))).strip()


def _codigo_cupom(texto: str) -> str:
    achado = _CUPOM.search(texto or "")
    if not achado:
        return ""
    codigo = achado.group(1).strip().upper()
    # Palavra comum grudada em "cupom" (ex.: "cupom disponivel") não é código.
    if codigo.isalpha() and len(codigo) < 6:
        return ""
    return codigo


class TelegramPublicoSource(SourceAdapter):
    slug = "telegram-publico"
    marketplace = "multiloja"
    name = "Telegram — canais públicos (prévia web)"
    requires_chromium = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def _canais(self, handles=None):
        if handles:
            return [str(h).strip().lstrip("@") for h in handles if str(h).strip()]
        return [c["handle"] for c in CANAIS_SUGERIDOS]

    def _baixar(self, handle):
        if not _HANDLE_OK.match(handle):
            logger.warning("Handle de canal recusado: %r", handle[:40])
            return ""
        resposta = requests.get(
            f"{BASE}{handle}", timeout=_TIMEOUT, headers={"User-Agent": _UA},
        )
        if resposta.status_code != 200:
            return ""
        return resposta.text or ""

    def _mensagens(self, corpo):
        """(post_id, texto) das mensagens da prévia, na ordem em que aparecem."""
        ids = _POST_ID.findall(corpo)
        blocos = _BLOCO_MENSAGEM.findall(corpo)
        # As duas listas costumam ter o mesmo tamanho; quando não têm, o id é opcional
        # e o texto é o que importa — melhor perder o id do que perder a mensagem.
        for indice, bloco in enumerate(blocos):
            post = ids[indice] if indice < len(ids) else ""
            yield post, _texto_limpo(bloco)

    def discover_offers(self, canais=None, **kwargs):
        handles = self._canais(canais)
        agora = timezone.now()
        vistos = set()
        lidos = falhas = 0

        for handle in handles[:12]:
            try:
                corpo = self._baixar(handle)
            except requests.RequestException as exc:
                falhas += 1
                logger.info("Canal @%s indisponível (%s).", handle, type(exc).__name__)
                continue
            if not corpo:
                falhas += 1
                continue
            lidos += 1
            for post, texto in self._mensagens(corpo):
                if not texto:
                    continue
                preco_alegado = 0.0
                achado_preco = _PRECO.search(texto)
                if achado_preco:
                    preco_alegado = normalizar_dinheiro(achado_preco.group(1))
                codigo = _codigo_cupom(texto)
                for url in _URL.findall(texto):
                    url = url.rstrip(").,;")
                    slug = _marketplace(url)
                    if not slug:
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
            # Nunca "completo": a prévia mostra só as mensagens recentes, então
            # ausência aqui não prova que a oferta sumiu e não pode expirar catálogo.
            "complete": False,
        }

    def discover_coupons(self, **kwargs):
        return []

    def healthcheck(self):
        return {"ok": True, "status": "ok"}
