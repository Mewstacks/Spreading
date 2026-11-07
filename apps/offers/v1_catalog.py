"""Importa catálogo e pares cupom+produto exportados do v1 (tools/export_catalog_v1.py e
tools/export_pairs_v1.py). Só leitura no v1; aqui tudo é validado de novo."""
import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from urllib.parse import parse_qs, urlparse

from django.utils import timezone

from apps.compose.image import safe_image_url
from apps.offers.feeds import is_prohibited

from . import niches
from .keys import product_key
from .links import safe_affiliate_url
from .models import AffiliateLink, Offer

ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)
CHUNK = 500


def _dec(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    return number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if number > 0 else None


def _when(text) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(text).replace(" ", "T", 1))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _pct(base, final) -> Decimal:
    if not base or not final or base <= final:
        return Decimal(0)
    return ((1 - final / base) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _store_key(row):
    store = row.get("marketplace")
    if store == "amazon":
        asin = (row.get("asin") or "").upper()
        if not asin:
            match = ASIN.search(row.get("url") or "")
            asin = match.group(1).upper() if match else ""
        return "amazon", asin
    if store == "mercadolivre":
        return "mercadolivre", product_key(row.get("url") or "")
    return None, ""


UPDATE_FIELDS = [
    "source", "product_key", "source_url", "title", "url", "image_url", "macro", "price_from",
    "price_to", "discount_pct", "last_seen", "coupon_kind", "coupon_code", "coupon_pct",
    "coupon_amount", "coupon_cap", "coupon_min", "coupon_until", "proof_base_price", "proof_at",
]


def _flush(batch: dict) -> int:
    """Grava o lote. A chave do dict é (loja, link_key, dia): o Postgres recusa um upsert que
    traga a mesma chave duas vezes no mesmo comando, então o lote já chega sem repetição."""
    if not batch:
        return 0
    Offer.objects.bulk_create(
        list(batch.values()), batch_size=CHUNK, update_conflicts=True,
        unique_fields=["store", "link_key", "valid_on"], update_fields=UPDATE_FIELDS,
    )
    return len(batch)


def import_catalog(rows: list[dict]) -> dict:
    """Mesmo produto visto várias vezes no mesmo dia vira uma oferta só: fica a observação
    mais recente (as demais contam em `descartados`)."""
    stats = {"importados": 0, "descartados": 0}
    batch: dict = {}
    for row in rows:
        store, key = _store_key(row)
        price_from, price_to = _dec(row.get("de")), _dec(row.get("efetivo"))
        seen = _when(row.get("visto"))
        image = safe_image_url(row.get("imagem") or "")
        title = str(row.get("nome") or "").strip()
        if (
            not store or not key or not seen or not image or not title or is_prohibited(title)
            or not price_to or not price_from or price_from <= price_to
        ):
            stats["descartados"] += 1
            continue
        url = str(row.get("url") or "")[:600]
        slot = (store, key, seen.date())
        held = batch.get(slot)
        if held is not None and held.last_seen > seen:
            stats["descartados"] += 1
            continue
        if held is not None:
            stats["descartados"] += 1  # a observação anterior do dia foi substituída
        batch[slot] = Offer(
            store=store, source="v1-catalogo", product_key=key, link_key=key,
            source_url=url, title=title[:300], url=url, image_url=image,
            macro=(row.get("macro") or niches.classify(title))[:80],
            price_from=price_from, price_to=price_to, discount_pct=_pct(price_from, price_to),
            valid_on=seen.date(), last_seen=seen,
        )
        if len(batch) >= CHUNK:
            stats["importados"] += _flush(batch)
            batch = {}
    stats["importados"] += _flush(batch)
    return stats


def _coupon_ref(row) -> tuple[str, str]:
    """(código, id da campanha) do par."""
    code = (row.get("codigo") or "").strip().upper()
    query = parse_qs(urlparse(row.get("link_cupom") or "").query)
    campaign = (query.get("coupon_campaign_id") or [""])[0]
    return code, campaign


def import_pairs(rows: list[dict], org=None) -> dict:
    stats = {"importados": 0, "descartados": 0, "links": 0}
    for row in rows:
        store, key = _store_key(row)
        original, base, final = _dec(row.get("preco_original")), _dec(row.get("preco_atual")), _dec(
            row.get("preco_final"))
        image = safe_image_url(row.get("imagem") or "")
        until, proof = _when(row.get("validade")), _when(row.get("verificado_em"))
        mode = "code" if row.get("redemption_mode") == "code" else "activation"
        code, campaign = _coupon_ref(row)
        title = str(row.get("nome") or "").strip()
        if (
            store != "mercadolivre" or not key or not image or not title or not final
            or not original or original <= final or not until or not proof or row.get("restrito")
            or is_prohibited(title) or (mode == "code" and not code)
            or (mode == "activation" and not campaign)
        ):
            stats["descartados"] += 1
            continue
        rules = row.get("regras") or {}
        is_pct = rules.get("tipo_desconto") == "porcentagem"
        value = _dec(rules.get("valor_desconto"))
        link_key = f"{key}+{code or 'c' + campaign}"
        source_url = row.get("link_cupom") or row.get("url") or ""
        Offer.objects.update_or_create(
            store="mercadolivre", link_key=link_key, valid_on=proof.date(),
            defaults={
                "source": "v1-pares", "product_key": key, "source_url": source_url[:1500],
                "title": title[:300], "url": str(row.get("url") or "")[:600], "image_url": image,
                "macro": (row.get("macro") or niches.classify(title))[:80],
                "price_from": original, "price_to": final, "discount_pct": _pct(original, final),
                "coupon_kind": mode, "coupon_code": code if mode == "code" else "",
                "coupon_pct": value if is_pct else None,
                "coupon_amount": None if is_pct else value,
                "coupon_cap": _dec(rules.get("desconto_maximo")),
                "coupon_min": _dec(rules.get("valor_minimo")),
                "coupon_until": until, "proof_base_price": base, "proof_at": proof,
                "last_seen": proof,
            },
        )
        stats["importados"] += 1
        affiliate = safe_affiliate_url(row.get("link_afiliado") or "")
        if org and affiliate and "/social/" not in affiliate:  # invariante: nunca o perfil social
            AffiliateLink.unscoped.update_or_create(
                org=org, product_key=link_key, defaults={"affiliate_url": affiliate}
            )
            stats["links"] += 1
    return stats


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
