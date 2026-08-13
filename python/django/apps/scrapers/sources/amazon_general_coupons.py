"""Códigos gerais Amazon de feed oficial/licenciado configurado pelo operador.

Não usa agregadores nem inventa códigos. A fonte permanece inativa até receber
uma URL HTTPS explícita em ``AMAZON_GENERAL_COUPONS_URL``.
"""
import hashlib
from datetime import datetime, time
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from apps.scrapers.coupon_rules import codigo_humano, normalizar_regras_cupom

from .base import IngestedItem, SourceAdapter


class AmazonGeneralCouponsSource(SourceAdapter):
    slug = "amazon-general-coupons"
    marketplace = "amazon"
    name = "Amazon — códigos gerais oficiais/licenciados"

    def __init__(self):
        self.last_metrics = {}
        self.last_health = "unknown"

    @staticmethod
    def configured():
        return bool(getattr(settings, "AMAZON_GENERAL_COUPONS_URL", ""))

    @staticmethod
    def _date(value, *, end=False):
        raw = str(value or "").strip()
        parsed = parse_datetime(raw)
        if parsed is None:
            day = parse_date(raw)
            if day:
                parsed = datetime.combine(day, time.max if end else time.min)
        if parsed is None:
            return None
        return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed

    def _rows(self):
        url = str(getattr(settings, "AMAZON_GENERAL_COUPONS_URL", "") or "")
        if not url.startswith("https://"):
            raise ValueError("Fonte Amazon de códigos gerais não configurada.")
        response = requests.get(url, timeout=20, headers={"User-Agent": "Spreading/1.0"})
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("coupons", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            raise ValueError("Inventário Amazon não é uma lista de cupons.")
        self.last_metrics = {
            "items_seen": len(rows), "complete": True,
            "schema_fingerprint": hashlib.sha256(
                "|".join(sorted({key for row in rows[:20] if isinstance(row, dict)
                                 for key in row})).encode("utf-8")
            ).hexdigest(),
        }
        self.last_health = "healthy_empty" if not rows else "healthy"
        return rows

    def discover_coupons(self, **kwargs):
        now = timezone.now()
        accepted = rejected = 0
        reasons = {}
        for row in self._rows():
            if not isinstance(row, dict):
                rejected += 1
                reasons["invalid_row"] = reasons.get("invalid_row", 0) + 1
                continue
            code = str(row.get("code") or row.get("codigo") or "").strip().upper()
            destination = str(row.get("url") or "https://www.amazon.com.br/").strip()
            try:
                parsed = urlsplit(destination)
            except ValueError:
                parsed = None
            hostname = (parsed.hostname or "").lower() if parsed else ""
            if (not codigo_humano(code) or not parsed or parsed.scheme != "https"
                    or not (hostname == "amazon.com.br"
                            or hostname.endswith(".amazon.com.br"))):
                rejected += 1
                reasons["invalid_identity"] = reasons.get("invalid_identity", 0) + 1
                continue
            rules = normalizar_regras_cupom({
                "tipo_desconto": row.get("discount_type") or row.get("tipo_desconto"),
                "valor_desconto": row.get("discount") or row.get("valor_desconto"),
                "valor_minimo": row.get("minimum_purchase") or row.get("valor_minimo"),
                "desconto_maximo": row.get("maximum_discount") or row.get("desconto_maximo"),
                "modo_resgate": "codigo",
                "is_mar_aberto": bool(row.get("sitewide", True)),
                "escopo": row.get("scope") or row.get("escopo") or "site inteiro",
                "dia_inicio": row.get("starts_at") or "",
                "dia_fim": row.get("valid_until") or "",
            }, external_id=f"amazon-code:{code}", codigo=code)
            if rules.get("valor_desconto") in (None, "", 0):
                rejected += 1
                reasons["invalid_discount"] = reasons.get("invalid_discount", 0) + 1
                continue
            valid_until = self._date(row.get("valid_until") or row.get("validade"), end=True)
            starts_at = self._date(row.get("starts_at") or row.get("inicio"))
            if valid_until is None or valid_until < now:
                rejected += 1
                reasons["invalid_end_date"] = reasons.get("invalid_end_date", 0) + 1
                continue
            accepted += 1
            yield IngestedItem(
                external_id=str(row.get("id") or f"amazon-code:{code}")[:160],
                marketplace="amazon", source=self.slug, kind="coupon",
                canonical_url=destination[:1000],
                title=str(row.get("title") or f"Cupom Amazon — {code}")[:255],
                coupon_code=code[:120], coupon_rules=rules,
                restricted=bool(row.get("restricted", False)), observed_at=now,
                starts_at=starts_at, valid_until=valid_until,
                evidence={"association": "amazon-official-or-licensed-code-feed",
                          "public": not bool(row.get("restricted", False))},
            )
        self.last_metrics.update({
            "accepted": accepted, "rejected": rejected, "rejections": reasons,
        })
