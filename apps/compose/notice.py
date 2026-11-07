"""Aviso de cupons (texto simples do WhatsApp). Só informação que está no feed."""
from decimal import Decimal

MAX_CODES = 3


def brl(value: Decimal) -> str:
    value = Decimal(value)
    if value == value.to_integral_value():
        whole = f"{int(value):,}".replace(",", ".")
        return f"R$ {whole}"
    text = f"{value:,.2f}"
    return "R$ " + text.replace(",", "_").replace(".", ",").replace("_", ".")


def pct(value: Decimal) -> str:
    value = Decimal(value)
    return f"{int(value)}%" if value == value.to_integral_value() else f"{value}%".replace(".", ",")


def coupon_line(coupon) -> str:
    parts = [f"*{coupon.code}*"]
    if coupon.discount_pct is not None:
        parts.append(f"{pct(coupon.discount_pct)} OFF")
    elif coupon.discount_amount is not None:
        parts.append(f"{brl(coupon.discount_amount)} OFF")
    if coupon.max_discount:
        parts.append(f"até {brl(coupon.max_discount)}")
    if coupon.min_purchase:
        parts.append(f"compra mín. {brl(coupon.min_purchase)}")
    parts.append(f"até {coupon.valid_until.strftime('%d/%m')}")
    return " · ".join(parts)


def build_coupon_notice(coupons, link: str) -> str:
    coupons = list(coupons)[:MAX_CODES]
    if not coupons:
        raise ValueError("aviso sem cupons")
    lines = ["🎟️ *Cupons Mercado Livre*", ""]
    lines += [coupon_line(c) for c in coupons]
    lines += ["", f"🔗 {link}"]
    return "\n".join(lines)
