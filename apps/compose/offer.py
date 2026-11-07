"""Mensagem de oferta no formato único do dono (golden do v1: FormatoUnicoTests).

    🛍️ *Título curto*

    🎟️ CUPOM: CÓDIGO            (ou `ative no link`)

    De ❌ ~R$ x~
    Por 🔥 *R$ y*

    Cupom: 30% OFF · até R$ 500 · aplique no carrinho
    ⏳ Válido até 30/11 às 23h59

    🔗 link

Só fatos provados; sem aviso de afiliação, sem LLM, sem comparação.
"""
import re
from zoneinfo import ZoneInfo

from .notice import brl, pct

BRT = ZoneInfo("America/Sao_Paulo")
_SEPARATORS = (" - ", " | ", " – ", ", ", " / ")
_CONNECTORS = {"de", "do", "da", "com", "para", "e", "a", "o", "em", "por", "sem", "ou", "no", "na"}
MIN_SHOWN = 5  # mínimo de compra só aparece acima de R$ 5


def short_name(title: str, limit: int = 50) -> str:
    """Nome curto (≤50), cortado por segmento e sem cauda pendurada."""
    text = " ".join(str(title or "").split())
    if len(text) <= limit:
        return text
    best = 0
    for sep in _SEPARATORS:
        for match in re.finditer(re.escape(sep), text):
            if match.start() <= limit:
                best = max(best, match.start())
    if best >= 20:
        return text[:best].strip(" ,-–|/")
    words = text[:limit].rsplit(" ", 1)[0].split()
    while words and words[-1].lower().strip(",-–|/") in _CONNECTORS:
        words.pop()
    return " ".join(words).strip(" ,-–|/")


def coupon_line(offer) -> str:
    if offer.coupon_kind == "code" and offer.coupon_code:
        return f"🎟️ CUPOM: {offer.coupon_code}"
    return "🎟️ CUPOM: ative no link"


def coupon_details(offer) -> str:
    parts = []
    if offer.coupon_pct is not None:
        parts.append(f"{pct(offer.coupon_pct)} OFF")
    elif offer.coupon_amount is not None:
        parts.append(f"{brl(offer.coupon_amount)} OFF")
    if offer.coupon_cap:
        parts.append(f"até {brl(offer.coupon_cap)}")
    if offer.coupon_min and offer.coupon_min > MIN_SHOWN:
        parts.append(f"compra mín. {brl(offer.coupon_min)}")
    if offer.coupon_kind == "code":
        parts.append("aplique no carrinho")
    return ("Cupom: " + " · ".join(parts)) if parts else ""


def build_offer_message(offer, link: str) -> str:
    blocks = [f"🛍️ *{short_name(offer.title)}*"]
    if offer.coupon_kind:
        blocks.append(coupon_line(offer))
    price = []
    if offer.price_from and offer.price_from > offer.price_to:
        price.append(f"De ❌ ~{brl(offer.price_from)}~")
    price.append(f"Por 🔥 *{brl(offer.price_to)}*")
    blocks.append("\n".join(price))
    if offer.coupon_kind:
        detail = []
        if coupon_details(offer):
            detail.append(coupon_details(offer))
        if offer.coupon_until:
            local = offer.coupon_until.astimezone(BRT)
            detail.append(f"⏳ Válido até {local:%d/%m} às {local:%H}h{local:%M}")
        if detail:
            blocks.append("\n".join(detail))
    blocks.append(f"🔗 {link}")
    return "\n\n".join(blocks)
