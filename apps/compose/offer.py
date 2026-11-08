"""Mensagem de oferta: o esqueleto único do dono (v1, FormatoUnicoTests) com linhas de fato.

    🛍️ *Título curto*

    🎟️ CUPOM: CÓDIGO            (ou `ative no link`)

    De ❌ ~R$ x~
    Por 🔥 *R$ y* (41% OFF)
    🚚 Frete grátis · 🏪 Loja oficial      (ML, quando o anúncio diz)
    ⭐ 4,9 · 584 vendidos                    (Shopee e Amazon, quando a loja informa)
    🏪 Nome da loja                          (Shopee)

    Cupom: 30% OFF · até R$ 500 · aplique no carrinho
    ⏳ Válido até 30/11 às 23h59

    🔗 link

Só fatos que a própria loja informa; linha sem dado não aparece. Sem aviso de afiliação, sem LLM,
sem comparação com outro produto (termos do ML, 5.4).
"""
import re
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .notice import brl, pct

BRT = ZoneInfo("America/Sao_Paulo")
_SEPARATORS = (" - ", " | ", " – ", ", ", " / ")
_CONNECTORS = {"de", "do", "da", "com", "para", "e", "a", "o", "em", "por", "sem", "ou", "no", "na"}
_NEEDS_MORE = {"air", "tamanho", "smart", "kit", "tipo", "modelo", "cor", "marca", "c/", "s/", "p/"}
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
    return _trim_tail(" ".join(words).strip(" ,-–|/"))


def _dangling(token: str) -> bool:
    bare = token.lower().strip(",-–|/.:;")
    if not re.search(r"\w", token):
        return True  # símbolo solto: "+", "&", "-"
    if len(bare) == 1 and bare.isalpha():
        return True  # letra solta: o "W" que sobrou de "W11"
    return bare in _CONNECTORS or bare in _NEEDS_MORE


def _trim_tail(text: str) -> str:
    """Tira da ponta o que o corte deixa pendurado: parêntese aberto, símbolo ou letra soltos,
    conectores e termos que pedem complemento ("Air" de "Air Fryer", "Tamanho"). Nome que ficaria
    com menos de duas palavras volta como estava."""
    out = text
    for opener, closer in (("(", ")"), ("[", "]")):
        if out.count(opener) > out.count(closer):
            out = out[: out.rindex(opener)]
    words = out.split()
    while len(words) > 2 and _dangling(words[-1]):
        words.pop()
    trimmed = " ".join(words).strip(" ,-–|/")
    return trimmed if len(trimmed.split()) >= 2 else text


def coupon_line(offer) -> str:
    if offer.coupon_kind == "code" and offer.coupon_code:
        return f"🎟️ CUPOM: {offer.coupon_code}"
    if getattr(offer, "store", "") == "amazon":
        return "🎟️ CUPOM: ative na página"  # caixinha "Aplicar cupom" no produto
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


def _plain(text, limit: int = 40) -> str:
    """Nome sem a marcação do WhatsApp (* _ ~ `) nem quebra de linha: não desmonta o negrito."""
    return " ".join(re.sub(r"[*_~`]+", " ", str(text or "")).split())[:limit].strip()


def _off(offer) -> str:
    """` (41% OFF)` quando há "De" acima do "Por" e a queda chega a 1%; senão nada."""
    before, now = offer.price_from, offer.price_to
    if not before or not now or before <= now:
        return ""
    drop = ((1 - now / before) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return f" ({int(drop)}% OFF)" if drop >= 1 else ""


def _rating(value) -> str:
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return ""
    if not 0 < number <= 5:
        return ""
    return str(number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)).replace(".", ",")


def _sales(value) -> str:
    """`584 vendidos`, `1,2 mil vendidos`, `12 mil vendidos` (para baixo: nunca infla)."""
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    if count == 1:
        return "1 vendido"
    if count < 1000:
        return f"{count} vendidos"
    if count < 10000:
        tenths = count // 100
        text = f"{tenths // 10},{tenths % 10}".removesuffix(",0")
    else:
        text = str(count // 1000)
    return f"{text} mil vendidos"


def facts(offer) -> list[str]:
    """Linhas de fato sob o preço; só entra o que a loja informou."""
    seller = _plain(getattr(offer, "seller", ""))
    flags = []
    if getattr(offer, "free_shipping", None) is True:
        flags.append("🚚 Frete grátis")
    if getattr(offer, "official_store", None) is True and not seller:
        flags.append("🏪 Loja oficial")  # o ML diz que é oficial, não diz o nome
    lines = [" · ".join(flags)] if flags else []
    rating = _rating(getattr(offer, "rating", None))
    if rating:
        sales = _sales(getattr(offer, "sales", None))
        lines.append(f"⭐ {rating}" + (f" · {sales}" if sales else ""))
    if seller:
        lines.append(f"🏪 {seller}")
    return lines


def build_offer_message(offer, link: str) -> str:
    blocks = [f"🛍️ *{short_name(offer.title)}*"]
    if offer.coupon_kind:
        blocks.append(coupon_line(offer))
    price = []
    if offer.price_from and offer.price_from > offer.price_to:
        price.append(f"De ❌ ~{brl(offer.price_from)}~")
    price.append(f"Por 🔥 *{brl(offer.price_to)}*{_off(offer)}")
    price.extend(facts(offer))
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
