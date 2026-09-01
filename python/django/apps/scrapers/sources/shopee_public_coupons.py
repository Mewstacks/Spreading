"""Cupons de ativacao da pagina publica oficial da Shopee Brasil.

A API de afiliados nao possui um endpoint de vouchers. A pagina publica, porem,
renderiza cada voucher com ``promotionId``, regras e estado ("Eu quero",
"Esgotado" ou "Ja utilizado"). Este adaptador coleta somente os disponiveis e
mantem cashback separado de desconto: moedas futuras nunca viram ``OFF``.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict
from urllib.parse import parse_qs, urljoin, urlsplit

from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from apps.scrapers.source_diagnostics import capture_public_diagnostic

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


COUPONS_URL = "https://shopee.com.br/m/cupom-de-desconto"
_OFF_PERCENT = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%\s*OFF\b", re.I)
_OFF_FIXED = re.compile(r"R\$\s*([\d.,]+\s*(?:mil)?)\s*OFF\b", re.I)
_MINIMUM = re.compile(r"(?:acima de|a partir de)\s*R\$\s*([\d.,]+\s*(?:mil)?)", re.I)
_MAXIMUM = re.compile(r"limitado a\s*R\$\s*([\d.,]+\s*(?:mil)?)", re.I)
_EXPECTED_REJECTIONS = {"unavailable", "cashback_not_discount", "duplicate"}


def _auth_required(url, body):
    folded = str(body or "").casefold()
    return bool(
        "/verify/traffic/error" in str(url or "").casefold()
        and (
            "login necessário" in folded
            or "login necessario" in folded
            or "faça login para continuar" in folded
            or "faca login para continuar" in folded
        )
    )


def _money(text):
    raw = str(text or "").strip().casefold().replace(" ", "")
    multiplier = 1000 if raw.endswith("mil") else 1
    if multiplier > 1:
        raw = raw[:-3]
    return round(normalizar_dinheiro(raw) * multiplier, 2)


def _parse_rendered_card(raw):
    """Converte um card renderizado; retorna ``(row, reason)`` auditavel."""
    text = str((raw or {}).get("text") or "").replace("\xa0", " ").strip()
    href = str((raw or {}).get("href") or "").strip()
    folded = text.casefold()
    if "eu quero" not in folded:
        return None, "unavailable"
    if "cashback" in folded:
        return None, "cashback_not_discount"

    parsed = urlsplit(href)
    promotion_id = (parse_qs(parsed.query).get("promotionId") or [""])[0].strip()
    if not promotion_id.isdigit():
        return None, "missing_promotion_id"

    fixed = _OFF_FIXED.search(text)
    percent = _OFF_PERCENT.search(text)
    if fixed:
        discount_type, discount = "fixo", _money(fixed.group(1))
    elif percent:
        discount_type = "porcentagem"
        discount = normalizar_dinheiro(percent.group(1))
    else:
        return None, "missing_discount"
    if discount <= 0 or (discount_type == "porcentagem" and discount >= 100):
        return None, "implausible_discount"

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    ignored = {"condicoes", "condições", "eu quero", "oficial", "indicado"}
    category = next((
        line for line in lines
        if line.casefold() not in ignored
        and not _OFF_FIXED.search(line) and not _OFF_PERCENT.search(line)
        and "cashback" not in line.casefold()
        and not _MINIMUM.search(line) and not _MAXIMUM.search(line)
    ), "Todas as lojas")
    minimum_match = _MINIMUM.search(text)
    maximum_match = _MAXIMUM.search(text)
    minimum = _money(minimum_match.group(1)) if minimum_match else None
    maximum = _money(maximum_match.group(1)) if maximum_match else None
    label = (
        f"{discount:g}% OFF" if discount_type == "porcentagem"
        else f"R$ {discount:g} OFF"
    )
    return {
        "promotion_id": promotion_id,
        "title": f"Cupom Shopee - {label} - {category}"[:255],
        "url": urljoin(COUPONS_URL, href),
        "category": category[:100],
        "discount_type": discount_type,
        "discount": discount,
        "minimum": minimum,
        "maximum": maximum,
        "restricted": tem_restricao_publico(text),
        "text": text[:1000],
    }, ""


def _snapshot_state(cards_count, accepted_count, rejected):
    """Classifica o snapshot sem apagar inventario diante de quebra de schema."""
    schema_errors = sum(
        count for reason, count in rejected.items()
        if reason not in _EXPECTED_REJECTIONS
    )
    complete = cards_count > 0 and schema_errors == 0
    if accepted_count:
        health = "healthy" if complete else "partial"
    else:
        health = "healthy_empty" if complete else "degraded"
    return complete, health, schema_errors


class ShopeePublicCouponsSource(SourceAdapter):
    slug = "shopee-public-coupons"
    marketplace = "shopee"
    name = "Shopee - cupons oficiais"
    requires_chromium = True

    def __init__(self):
        self.last_metrics = {}
        self.last_health_status = "unknown"

    def discover_coupons(self, usuario=None, **kwargs):
        rejected = defaultdict(int)
        accepted = []
        started = time.monotonic()
        state = None
        if usuario is not None:
            from apps.scrapers.report_sessions import (
                has_report_session, load_report_state,
            )
            if not has_report_session(usuario, "shopee_shop"):
                self.last_metrics = {
                    "items_seen": 0, "accepted": 0, "rejected": 0,
                    "complete": False, "reason_code": "auth_required",
                    "pages_processed": 0,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
                self.last_health_status = "auth_required"
                return
            try:
                state = load_report_state(usuario, "shopee_shop")
            except ValueError:
                self.last_metrics = {
                    "items_seen": 0, "accepted": 0, "rejected": 0,
                    "complete": False, "reason_code": "auth_required",
                    "pages_processed": 0,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
                self.last_health_status = "auth_required"
                return
        refreshed_state = None
        auth_required = False
        with iniciar_browser(storage_state=state, headless=True) as (page, context):
            page.goto(COUPONS_URL, wait_until="domcontentloaded", timeout=45000)
            # O HTML inicial contem apenas o shell; os vouchers chegam no hydrate.
            # Esperar o contrato semantico evita declarar schema quebrado numa VM
            # que levou alguns segundos a mais para executar o bundle.
            try:
                page.wait_for_selector(
                    "a[href*='/voucher/details']", timeout=12000,
                )
                # O primeiro bloco aparece antes dos demais carrosseis. Uma curta
                # janela de estabilizacao impede que 3 cards sejam tratados como
                # inventario completo quando a pagina termina com 10+.
                page.wait_for_timeout(3000)
            except Exception:
                pass
            body = page.locator("body").inner_text(timeout=10000)
            folded = body.casefold()
            auth_required = _auth_required(page.url, body)
            if auth_required:
                capture_public_diagnostic(page, self.slug, "auth_required")
                cards = []
            else:
                cards = None
            if any(marker in folded for marker in (
                    "verifique que voce e humano", "verifique que você é humano",
                    "access denied", "captcha")):
                capture_public_diagnostic(page, self.slug, "captcha_or_block")
                self.last_health_status = "blocked"
                raise RuntimeError("captcha")

            # Classes CSS da Shopee sao ofuscadas e mudam a cada build. O contrato
            # estavel e o link publico dos termos; subimos ate o menor ancestral
            # que tambem contem desconto e estado do voucher.
            if cards is None:
                cards = page.locator("a[href*='/voucher/details']").evaluate_all("""
                anchors => anchors.map(a => {
                  let node = a;
                  while (node && node !== document.body) {
                    const text = (node.innerText || '').trim();
                    const hasDiscount = /(?:R\\$\\s*[\\d.,]+\\s*(?:mil)?\\s*OFF|[\\d.,]+\\s*%\\s*OFF|CASHBACK)/i.test(text);
                    const hasState = /(?:Eu quero|Esgotado|J[aá] utilizado)/i.test(text);
                    if (hasDiscount && hasState && text.length < 600) {
                      const img = node.querySelector('img');
                      return {text, href: a.getAttribute('href') || '', image: img ? (img.currentSrc || img.src || '') : ''};
                    }
                    node = node.parentElement;
                  }
                  return {text: '', href: a.getAttribute('href') || '', image: ''};
                })
            """)
            seen = set()
            for raw in cards:
                row, reason = _parse_rendered_card(raw)
                if row is None:
                    rejected[reason or "invalid"] += 1
                    continue
                if row["promotion_id"] in seen:
                    rejected["duplicate"] += 1
                    continue
                seen.add(row["promotion_id"])
                row["image"] = str(raw.get("image") or "").split("?", 1)[0][:1000]
                accepted.append(row)
            if not cards and not auth_required:
                capture_public_diagnostic(page, self.slug, "voucher_cards_not_found")
            if usuario is not None and not auth_required:
                refreshed_state = context.storage_state()

        if refreshed_state is not None:
            from apps.scrapers.report_sessions import save_report_state
            save_report_state(usuario, "shopee_shop", refreshed_state)

        complete, health, schema_errors = _snapshot_state(
            len(cards), len(accepted), rejected,
        )
        if auth_required:
            complete, health, schema_errors = False, "auth_required", 0
        self.last_metrics = {
            "items_seen": len(cards),
            "accepted": len(accepted),
            "rejected": sum(rejected.values()),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": complete,
            "reason_code": "auth_required" if auth_required else "",
            "schema_errors": schema_errors,
            "pages_processed": 1,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "schema_fingerprint": hashlib.sha256(
                "voucher-details|discount|state".encode("utf-8")
            ).hexdigest(),
        }
        self.last_health_status = health
        observed = timezone.now()
        for row in accepted:
            rules = normalizar_regras_cupom({
                "tipo_desconto": row["discount_type"],
                "valor_desconto": row["discount"],
                "valor_minimo": row["minimum"],
                "desconto_maximo": row["maximum"],
                "modo_resgate": "ativacao",
                "escopo": row["category"],
            }, external_id=f"shopee-voucher:{row['promotion_id']}")
            yield IngestedItem(
                external_id=f"shopee-voucher:{row['promotion_id']}",
                marketplace=self.marketplace,
                source=self.slug,
                kind="coupon",
                canonical_url=row["url"],
                title=row["title"],
                image_url=row["image"],
                coupon_rules=rules,
                content_type="promotion",
                restricted=row["restricted"],
                observed_at=observed,
                evidence={
                    "transport": "shopee-official-coupon-page",
                    "association": "shopee-official-coupon-page",
                    "promotion_id": row["promotion_id"],
                    "availability": "claimable",
                    "snapshot": row["text"],
                },
            )

    def healthcheck(self):
        try:
            return {"ok": bool(list(self.discover_coupons()))}
        except Exception as exc:
            return {"ok": False, "erro": "Falha temporaria na fonte publica.",
                    "cause": type(exc).__name__}
