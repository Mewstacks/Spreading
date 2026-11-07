"""Importa o JSON gerado por tools/export_v1.py (dados não secretos do v1)."""
import json
from decimal import Decimal

from django.db import transaction

from apps.coupons.feed import safe_container_url
from apps.offers.keys import product_key
from apps.offers.links import safe_affiliate_url
from apps.offers.models import AffiliateLink, Rule

from .models import ContainerLink, Destination, Organization


def _destination(org, rule_row):
    external = str(rule_row["grupo_id"]).strip()
    kind = Destination.WA_CHANNEL if external.endswith("@newsletter") else Destination.WA_GROUP
    dest, _ = Destination.unscoped.get_or_create(
        org=org, kind=kind, external_id=external,
        defaults={"name": rule_row.get("grupo_nome") or external},
    )
    return dest


def _num(value):
    return None if value in (None, "") else Decimal(str(value))


@transaction.atomic
def import_v1(data: dict, slug_by_name=None) -> dict:
    stats = {"orgs": 0, "regras": 0, "regras_puladas": 0, "containers": 0, "produtos": 0,
             "links_rejeitados": 0}
    for row in data.get("orgs", []):
        slug = (slug_by_name or {}).get(row["name"]) or row["name"].lower().replace(" ", "-")
        org, _ = Organization.objects.get_or_create(slug=slug, defaults={"name": row["name"]})
        stats["orgs"] += 1

        for rule in row.get("rules", []):
            supported = rule.get("canal") == "whatsapp" and rule.get("marketplace") in (
                "", "mercadolivre")
            if not supported:
                stats["regras_puladas"] += 1  # Telegram/Amazon/Shopee ficam para depois
                continue
            dest = _destination(org, rule)
            kind = Rule.COUPON_NOTICE if rule["tipo"] == "aviso_cupons" else Rule.OFFERS
            name = f"v1-{rule['id']}"
            twin = Rule.unscoped.filter(org=org, destination=dest, type=kind).exclude(name=name)
            if twin.exists():
                stats["regras_puladas"] += 1  # o painel configura um grupo por vez
                continue
            # Só cria: depois da primeira importação a regra é do painel e reimportar não a desfaz.
            Rule.unscoped.get_or_create(
                org=org, name=name,
                defaults={
                    "destination": dest,
                    "type": kind,
                    "terms": rule.get("termo_busca") or "",
                    "negative_terms": rule.get("termos_negativos") or "",
                    "price_min": _num(rule.get("preco_min")),
                    "price_max": _num(rule.get("preco_max")),
                    "min_discount_pct": Decimal(str(rule.get("min_desconto_percent") or 15)),
                    "window_start": rule.get("janela_inicio", 8),
                    "window_end": rule.get("janela_fim", 20),
                    "weekdays": rule.get("dias_semana") or "",
                    "interval_minutes": max(1, int(rule.get("intervalo_minutos") or 60)),
                    "daily_cap": int(rule.get("max_envios_dia") or 12),
                    "cooldown_hours": int(rule.get("horas_cooldown") or 360),
                    "active": bool(rule.get("ativo", True)),
                },
            )
            stats["regras"] += 1

        for link in row.get("container_links", []):
            container = safe_container_url(link.get("container_url", ""))
            affiliate = safe_affiliate_url(link.get("affiliate_url", ""))
            if not container or not affiliate:
                stats["links_rejeitados"] += 1
                continue
            ContainerLink.unscoped.update_or_create(
                org=org, container_url=container, defaults={"affiliate_url": affiliate}
            )
            stats["containers"] += 1

        existing = set(AffiliateLink.unscoped.filter(org=org).values_list("product_key", flat=True))
        batch = []
        for link in row.get("product_links", []):
            affiliate = safe_affiliate_url(link.get("affiliate_url", ""))
            if not affiliate or link.get("verificado") is not True:
                stats["links_rejeitados"] += 1  # vitrine, host estranho ou sem veredito do v1
                continue
            key = product_key(link.get("url", ""))
            if not key or key in existing:
                continue
            existing.add(key)
            batch.append(AffiliateLink(org=org, product_key=key, affiliate_url=affiliate))
        AffiliateLink.unscoped.bulk_create(batch, batch_size=1000, ignore_conflicts=True)
        stats["produtos"] += len(batch)
    return stats


def import_file(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return import_v1(json.load(handle))
