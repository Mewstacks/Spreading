"""Cupons-relampago publicados na pagina oficial de cupons do Mercado Livre.

O bloco ``lightning-coupons-*`` do payload Nordic informa a agenda do dia com
codigo, campanha, inicio, fim, desconto e estado. A coleta e HTTP e leve: pode
rodar na lane de cinco minutos sem disputar o unico Chromium da aplicacao.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from datetime import timedelta

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.scrapers.coupon_rules import (
    codigo_humano, normalizar_regras_cupom, tem_restricao_publico,
)
from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import _payload_nordic

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


COUPONS_URL = "https://www.mercadolivre.com.br/ofertas/cupons"
_TIMEOUT = (5, 20)
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%\s*OFF", re.I)
_FIXED = re.compile(r"R\$\s*([\d.,]+)\s*OFF", re.I)
_TERMINAL = {"CANCELLED", "EXPIRED", "FINISHED", "INACTIVE", "UNAVAILABLE"}


def _aware_datetime(value):
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return None
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _money(value):
    return normalizar_dinheiro(str(value or ""))


def _lightning_contract(payload):
    """Retorna os cards e se o bloco oficial foi reconhecido no payload."""
    cards, seen = [], set()
    contract_found = False

    def visit(value, *, lightning_parent=False):
        nonlocal contract_found
        if isinstance(value, list):
            for child in value:
                visit(child, lightning_parent=lightning_parent)
            return
        if not isinstance(value, dict):
            return
        is_lightning = lightning_parent
        if is_lightning:
            contract_found = True
        signature = {
            "campaign_id", "coupon_redeem_type", "start_date",
            "expiration_date", "title",
        }
        if is_lightning and signature <= set(value):
            campaign_id = str(value.get("campaign_id") or "").strip()
            if campaign_id and campaign_id not in seen:
                seen.add(campaign_id)
                cards.append(value)
            return
        for key, child in value.items():
            child_is_lightning = is_lightning or str(key).startswith(
                "lightning-coupons-"
            )
            if child_is_lightning:
                contract_found = True
            visit(child, lightning_parent=child_is_lightning)

    visit(payload)
    return cards, contract_found


def extract_lightning_coupons(payload, *, now=None):
    """Normaliza apenas cupons futuros/ativos; nunca revive item finalizado."""
    now = now or timezone.now()
    cards, contract_found = _lightning_contract(payload)
    accepted = []
    rejected = defaultdict(int)
    latest_expiration = None
    for raw in cards:
        status = str((raw.get("status") or {}).get("id") or "").upper()
        start = _aware_datetime(raw.get("start_date"))
        expiration = _aware_datetime(raw.get("expiration_date"))
        if expiration and (latest_expiration is None or expiration > latest_expiration):
            latest_expiration = expiration
        if status in _TERMINAL or (expiration and expiration <= now):
            rejected["finished"] += 1
            continue
        if start is None or expiration is None or expiration <= start:
            rejected["invalid_window"] += 1
            continue
        if str(raw.get("coupon_redeem_type") or "").upper() != "CODE":
            rejected["not_a_code"] += 1
            continue
        code = codigo_humano(str(raw.get("code") or "").strip().upper())
        if not code:
            rejected["invalid_code"] += 1
            continue
        title_text = str((raw.get("title") or {}).get("text") or "").strip()
        percent = _PERCENT.search(title_text)
        fixed = _FIXED.search(title_text)
        if percent:
            discount_type = "porcentagem"
            discount = normalizar_dinheiro(percent.group(1))
        elif fixed:
            discount_type = "fixo"
            discount = normalizar_dinheiro(fixed.group(1))
        else:
            rejected["missing_discount"] += 1
            continue
        if discount <= 0 or (discount_type == "porcentagem" and discount >= 100):
            rejected["implausible_discount"] += 1
            continue
        category = str(raw.get("category") or "produtos selecionados").strip()
        category = re.sub(r"^em\s+", "", category, flags=re.I).strip()
        amount = raw.get("amount") or {}
        conditions = str((raw.get("conditions") or {}).get("text") or "")
        conditions += " " + str(
            ((raw.get("conditions") or {}).get("accessibility") or {}).get(
                "sr_label"
            ) or ""
        )
        rules = normalizar_regras_cupom({
            "tipo_desconto": discount_type,
            "valor_desconto": discount,
            "valor_minimo": _money(amount.get("min_amount")),
            "desconto_maximo": _money(amount.get("cap_amount")),
            "modo_resgate": "codigo",
            "escopo": category,
            "is_mar_aberto": False,
            "dia_inicio": start.isoformat(),
            "dia_fim": expiration.isoformat(),
        }, external_id=f"ml-lightning:{raw['campaign_id']}", codigo=code)
        total_items = (raw.get("segmentations") or {}).get("total_items")
        accepted.append(IngestedItem(
            external_id=f"ml-lightning:{raw['campaign_id']}"[:160],
            marketplace="mercadolivre", source="ml-lightning-coupons",
            kind="coupon", canonical_url=COUPONS_URL,
            title=f"Cupom relampago {code} - {title_text} - {category}"[:255],
            coupon_code=code, coupon_rules=rules, content_type="voucher",
            starts_at=start, valid_until=expiration,
            restricted=tem_restricao_publico(conditions), flash=True,
            observed_at=now,
            evidence={
                "transport": "mercadolivre-official-lightning-payload",
                "association": "mercadolivre-official-coupon-page",
                "promotion_id": str(raw["campaign_id"]),
                "status": status or "UNKNOWN",
                "start_time": str(raw.get("start_time") or ""),
                "total_items": total_items,
            },
        ))
    stale = bool(
        cards and latest_expiration
        and latest_expiration < now - timedelta(days=2)
    )
    metrics = {
        "items_seen": len(cards),
        "accepted": len(accepted),
        "rejected": sum(rejected.values()),
        "rejected_by_reason": dict(sorted(rejected.items())),
        "contract_found": contract_found,
        "stale_inventory": stale,
        "latest_expiration": latest_expiration.isoformat() if latest_expiration else "",
        "schema_fingerprint": hashlib.sha256("|".join(sorted({
            str(key) for card in cards for key in card
        })).encode("utf-8")).hexdigest(),
        # A pagina e agenda do dia, nao um catalogo historico autoritativo.
        "complete": False,
    }
    if not contract_found or stale:
        health = "degraded"
    elif accepted:
        health = "healthy"
    else:
        health = "healthy_empty"
    return accepted, metrics, health


class MLLightningCouponsSource(SourceAdapter):
    slug = "ml-lightning-coupons"
    marketplace = "mercadolivre"
    name = "Mercado Livre - cupons relampago oficiais"
    requires_chromium = False
    inventario_completo = False

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def discover_coupons(self, **kwargs):
        started = time.monotonic()
        response = requests.get(
            COUPONS_URL, timeout=_TIMEOUT, headers={"User-Agent": _UA},
        )
        response.raise_for_status()
        payload = _payload_nordic(response.text)
        rows, self.last_metrics, self.last_health_status = extract_lightning_coupons(
            payload or {}, now=timezone.now(),
        )
        self.last_metrics["duration_ms"] = round(
            (time.monotonic() - started) * 1000
        )
        self.last_metrics["pages_processed"] = 1
        yield from rows

    def healthcheck(self):
        return {
            "ok": self.last_health_status in {"healthy", "healthy_empty"},
            "health": self.last_health_status,
            "metrics": self.last_metrics,
        }
