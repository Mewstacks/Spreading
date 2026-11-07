"""Mensagem de oferta no formato único definido pelo dono (22/10). Só fatos do anúncio/feed."""
from .notice import brl


def build_offer_message(offer, link: str) -> str:
    lines = [f"🛍️ *{offer.title.strip()}*"]
    if offer.coupon_code:
        lines.append(f"🎟️ CUPOM: {offer.coupon_code}")
    if offer.price_from and offer.price_from > offer.price_to:
        lines.append(f"De ❌ ~{brl(offer.price_from)}~")
    lines.append(f"Por 🔥 *{brl(offer.price_to)}*")
    lines.append(f"⏳ Válido até {offer.valid_on.strftime('%d/%m')}")
    lines.append(f"🔗 {link}")
    return "\n".join(lines)
