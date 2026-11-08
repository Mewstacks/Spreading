"""Colar produto e cupom: o dono cola links e o v2 traz a oferta pelas fontes oficiais.

Colar só substitui a descoberta: preço, "De", foto e vendedor vêm da API do ML, da API da Shopee ou
da busca da Amazon, e o que cada fonte exige de prova continua valendo (sem "De" no anúncio não há
oferta). Cupom manual existe só no ML: o dono informa código, valor, mínimo, teto e validade, e o
par nasce provado pelo preço ao vivo daquele instante; no envio, a prova é refeita como em qualquer
outro par. Link curto (`meli.la`, `s.shopee.com.br`) não é aceito como produto: o redirecionamento
seria um clique automatizado do dono (termos do ML, cláusula 1.9).
"""
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from time import monotonic
from urllib.parse import urlparse

from django.utils import timezone

from apps.compose.image import safe_image_url
from apps.coupons.feed import BRT

from . import amazon, ml_api, niches, selection, shopee
from .catalog import REFRESH_EVERY
from .feeds import is_prohibited
from .keys import product_key
from .links import safe_affiliate_url
from .models import AffiliateLink, Offer, TermRun

SOURCE = "manual"
MAX_LINES = 20
DEADLINE_S = 20  # o web tem 2 workers e 30 s de limite: lote lento para antes de matar o worker
MAX_PCT = Decimal(90)
URL = re.compile(r"https?://[^\s<>\"']+")
ML_CATALOG = re.compile(r"^MLB\d+$")
ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", re.I)
SHOPEE_ITEM = re.compile(r"(?:/product/\d+/|-i\.\d+\.)(\d{4,20})(?:/|$)")
CODE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
SHORT_HOSTS = ("meli.la", "s.shopee.com.br", "shp.ee", "shope.ee", "amzn.to", "a.co")
REASONS = {
    "link_invalido": "Link inválido", "link_curto": "Link curto",
    "loja_desconhecida": "Loja desconhecida",
    "anuncio_solto": "Anúncio solto", "preco_inconclusivo": "Preço inconclusivo",
    "inativo": "Produto inativo", "sem_desconto": "Sem desconto", "sem_foto": "Sem foto",
    "categoria_proibida": "Categoria vedada", "nao_atende": "Fora dos critérios",
    "nao_encontrado": "Não encontrado", "sem_tag": "Sem tag Amazon",
    "amazon_desligada": "Amazon desligada", "amazon_bloqueou": "Amazon bloqueou",
    "shopee_desligada": "Shopee desligada", "shopee_falhou": "Shopee falhou",
    "abaixo_do_minimo": "Abaixo do mínimo", "cupom_invalido": "Cupom inválido",
    "cupom_so_ml": "Cupom só no ML", "tempo_esgotado": "Tempo esgotado",
}


@dataclass
class Coupon:
    code: str = ""  # vazio = cupom de ativação (o link do dono é que carrega a campanha)
    kind: str = "pct"  # pct | amount
    value: Decimal = Decimal(0)
    minimum: Decimal | None = None
    cap: Decimal | None = None
    until: date | None = None


@dataclass
class Outcome:
    url: str
    status: str = "erro"  # ok | sem_link | erro
    reason: str = ""
    store: str = ""
    key: str = ""
    title: str = ""
    price: Decimal | None = None
    offer: Offer | None = None


@dataclass
class Report:
    outcomes: list = field(default_factory=list)
    ignored: int = 0


def _lines(text) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def parse(text) -> list[tuple[str, str]]:
    """Até MAX_LINES linhas `produto [link do dono]`; linha sem link volta crua e é recusada."""
    rows = []
    for line in _lines(text)[:MAX_LINES]:
        urls = URL.findall(line)
        rows.append((urls[0], urls[1] if len(urls) > 1 else "") if urls else (line[:200], ""))
    return rows


def _is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def identify(url: str) -> tuple[str, str, str]:
    """(loja, chave do produto, motivo); a chave vem vazia quando o link não se prova."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return "", "", "link_invalido"
    if host in SHORT_HOSTS:
        return "", "", "link_curto"
    if _is(host, "mercadolivre.com.br"):
        key = product_key(url)
        if ML_CATALOG.match(key) and "/p/" in parsed.path.lower():
            return "mercadolivre", key, ""
        return "", "", "anuncio_solto"  # anúncio solto: a API do ML não mede preço
    if _is(host, "amazon.com.br"):
        match = ASIN.search(parsed.path)
        return ("amazon", match.group(1).upper(), "") if match else ("", "", "link_invalido")
    if _is(host, "shopee.com.br"):
        match = SHOPEE_ITEM.search(parsed.path)
        return ("shopee", match.group(1), "") if match else ("", "", "link_invalido")
    return "", "", "loja_desconhecida"


def _money(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if number > 0 else None


def _coupon_ok(coupon: Coupon, today: date) -> bool:
    if coupon.value <= 0 or (coupon.kind == "pct" and coupon.value > MAX_PCT):
        return False
    if coupon.kind not in ("pct", "amount") or coupon.until is None or coupon.until < today:
        return False
    return not coupon.code or bool(CODE.match(coupon.code))


def _save_link(org, link_key: str, owner_link: str):
    url = safe_affiliate_url(owner_link) if owner_link else ""
    if url:
        AffiliateLink.unscoped.update_or_create(
            org=org, product_key=link_key, defaults={"affiliate_url": url, "etiqueta": SOURCE})


def _ml(org, key, coupon, owner_link, now, http):
    info = ml_api.product(key, http=http)
    live = ml_api.product_price(key, http=http)
    if not info or not live or not live.get("price"):
        return None, "preco_inconclusivo"
    if info.get("status") != "active":
        return None, "inativo"
    title, image = str(info["name"]).strip(), safe_image_url(info.get("image") or "")
    if not image:
        return None, "sem_foto"
    if is_prohibited(title):
        return None, "categoria_proibida"
    base, listed = _money(live["price"]), _money(live.get("price_from"))
    original = listed if listed and listed > base else None
    link_key, fields = key, {
        "coupon_kind": "", "coupon_code": "", "coupon_pct": None, "coupon_amount": None,
        "coupon_cap": None, "coupon_min": None, "coupon_until": None,
        "proof_base_price": None, "proof_at": None,
    }
    if coupon is None:
        if not original:
            return None, "sem_desconto"  # sem "De" no anúncio a mensagem não tem o que provar
        final, price_from = base, original
    else:
        if coupon.minimum and base < coupon.minimum:
            return None, "abaixo_do_minimo"
        discount = base * coupon.value / 100 if coupon.kind == "pct" else coupon.value
        if coupon.cap:
            discount = min(discount, coupon.cap)
        discount = discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        final = base - discount
        if discount <= 0 or final <= 0:
            return None, "cupom_invalido"
        price_from, link_key = original or base, f"{key}+{coupon.code or 'ativar'}"
        fields.update(
            coupon_kind="code" if coupon.code else "activation", coupon_code=coupon.code,
            coupon_pct=coupon.value if coupon.kind == "pct" else None,
            coupon_amount=coupon.value if coupon.kind == "amount" else None,
            coupon_cap=coupon.cap, coupon_min=coupon.minimum,
            coupon_until=datetime.combine(coupon.until, time(23, 59, 59), tzinfo=BRT),
            proof_base_price=base, proof_at=now)
    pct = ((1 - final / price_from) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    url = f"https://www.mercadolivre.com.br/p/{key}"
    offer, _created = Offer.objects.update_or_create(
        store="mercadolivre", link_key=link_key, valid_on=now.astimezone(BRT).date(),
        defaults={
            "source": SOURCE, "product_key": key, "source_url": url, "title": title[:300],
            "url": url, "image_url": image, "macro": niches.classify(title),
            "price_from": price_from, "price_to": final, "discount_pct": pct, "last_seen": now,
            **fields,
        })
    _save_link(org, link_key, owner_link)
    return offer, ""


def _shopee(org, key, coupon, owner_link, now, http):
    if not shopee.configured():
        return None, "shopee_desligada"
    try:
        node = shopee.fetch_item(int(key), http=http)
    except shopee.ShopeeError:
        return None, "shopee_falhou"
    if node is None:
        return None, "nao_encontrado"
    row = shopee.vet(node)
    if row is None:
        return None, "nao_atende"
    today = now.astimezone(BRT).date()
    shopee.store(org, row, now, today)  # grava a oferta e o link comissionado que a API devolveu
    return Offer.objects.get(store="shopee", link_key=row["key"], valid_on=today), ""


def _amazon(org, asin, coupon, owner_link, now, http):
    if not amazon.enabled():
        return None, "amazon_desligada"
    if not org.amazon_tag:
        return None, "sem_tag"
    paused = (TermRun.objects.filter(term=amazon.BLOCK_KEY)
              .values_list("last_run", flat=True).first())
    if paused and now - paused < REFRESH_EVERY:
        return None, "amazon_bloqueou"
    try:
        html = amazon.fetch(asin, http)
    except amazon.AmazonBlocked:
        TermRun.objects.update_or_create(term=amazon.BLOCK_KEY, defaults={"last_run": now})
        return None, "amazon_bloqueou"
    card = next((c for c in amazon.parse(html) if c["asin"] == asin), None)
    if card is None:
        return None, "nao_encontrado"
    row = amazon.vet(card)
    if row is None:
        return None, "nao_atende"
    return amazon.store(row, now, now.astimezone(BRT).date()), ""


STORES = {"mercadolivre": _ml, "shopee": _shopee, "amazon": _amazon}


def _has_link(org, offer) -> bool:
    keys = {offer.link_key, offer.product_key}
    links = dict(AffiliateLink.objects.for_org(org).filter(product_key__in=keys)
                 .values_list("product_key", "affiliate_url"))
    return bool(selection.resolve_link(org, offer, links))


def _one(org, raw, owner_link, coupon, now, http) -> Outcome:
    out = Outcome(url=raw[:200])
    store, key, reason = identify(raw)
    out.store = store
    if not reason and coupon and store != "mercadolivre":
        reason = "cupom_so_ml"
    if not reason and coupon and not _coupon_ok(coupon, now.astimezone(BRT).date()):
        reason = "cupom_invalido"
    offer = None
    if not reason:
        offer, reason = STORES[store](org, key, coupon, owner_link, now, http)
    if reason:
        out.reason = reason
        return out
    out.offer, out.key, out.title, out.price = offer, offer.product_key, offer.title, offer.price_to
    out.status = "ok" if _has_link(org, offer) else "sem_link"
    return out


def add(org, text, coupon: Coupon | None = None, now=None, http=None, clock=monotonic,
        deadline=DEADLINE_S) -> Report:
    """Uma saída por linha colada (até MAX_LINES); `ignored` conta as que passaram do limite.
    Passou de `deadline` segundos, as linhas que faltam voltam como `tempo_esgotado`: colar de
    novo é seguro, a oferta do dia é a mesma."""
    now = now or timezone.now()
    if coupon is not None:
        coupon = replace(coupon, code=(coupon.code or "").strip().upper())
    report = Report(ignored=max(0, len(_lines(text)) - MAX_LINES))
    started = clock()
    for raw, owner_link in parse(text):
        if clock() - started > deadline:
            report.outcomes.append(Outcome(url=raw[:200], reason="tempo_esgotado"))
            continue
        report.outcomes.append(_one(org, raw, owner_link, coupon, now, http))
    return report
