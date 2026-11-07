"""Prova de preço ao vivo no envio (ML): inconclusivo não publica."""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from . import ml_api
from .selection import is_live_checkable

TOLERANCE = Decimal("0.01")


@dataclass
class Live:
    price_to: Decimal
    price_from: Decimal | None
    discount_pct: Decimal


def _dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def validate(offer, min_discount, http=None) -> tuple[Live | None, str]:
    """(Live, '') se publicável agora; (None, motivo) caso contrário."""
    if not is_live_checkable(offer):
        return Live(offer.price_to, offer.price_from, offer.discount_pct), ""
    data = ml_api.product_price(offer.product_key, http=http)
    if not data or not data.get("price"):
        return None, "preco_inconclusivo"
    live = _dec(data["price"])
    if offer.coupon_kind:
        # Preço do cupom só vale se o anúncio ainda está no preço da prova.
        if offer.proof_base_price is None or abs(live - offer.proof_base_price) > TOLERANCE:
            return None, "preco_diverge_da_prova"
        final = offer.price_to
        base = offer.price_from or offer.proof_base_price
    else:
        final = live
        # O "De" que vale é o que o anúncio mostra agora; a observação antiga só serve quando a
        # API não informa o preço original.
        base = _dec(data["price_from"]) if data.get("price_from") else offer.price_from
    if not base or base <= final:
        return None, "sem_desconto"
    pct = ((1 - final / base) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if pct < Decimal(min_discount):
        return None, "desconto_caiu"
    return Live(final, base, pct), ""
