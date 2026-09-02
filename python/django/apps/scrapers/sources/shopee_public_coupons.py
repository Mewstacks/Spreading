"""Cupons de ativacao da pagina publica oficial da Shopee Brasil.

A API de afiliados nao possui um endpoint de vouchers. A pagina publica, porem,
renderiza cada voucher com ``promotionId``, regras e estado ("Eu quero",
"Esgotado" ou "Ja utilizado"). Este adaptador coleta somente os disponiveis e
mantem cashback separado de desconto: moedas futuras nunca viram ``OFF``.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from django.conf import settings
from django.utils import timezone

from apps.scrapers.auxiliar import iniciar_browser
from apps.scrapers.coupon_rules import normalizar_regras_cupom, tem_restricao_publico
from apps.scrapers.source_diagnostics import capture_public_diagnostic

from .base import IngestedItem, SourceAdapter, normalizar_dinheiro


COUPONS_URL = "https://shopee.com.br/m/cupom-de-desconto"
# A Shopee troca o sufixo desta microsite sem redirecionar a rota anterior. A
# vitrine publicada e indexada em 02/09/2026 é a v98; manter a v39 aqui fazia o
# Chromium autenticar normalmente, mas consultar uma coleção editorial antiga.
DAILY_STORE_COUPONS_URL = "https://shopee.com.br/m/cupom-de-desconto-v98"
# A rota diária é uma navegação de aquecimento barata: na Fly a Shopee por vezes
# desafia a primeira página do contexto, mas reconhece a conta na seguinte. Abrir
# a coleção de vendedores antes da vitrine geral reduz esse falso bloqueio sem
# proxy e sem alterar a sessão persistida.
COUPON_PAGES = (DAILY_STORE_COUPONS_URL, COUPONS_URL)
_OFF_PERCENT = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*%\s*OFF\b", re.I)
_OFF_FIXED = re.compile(r"R\$\s*([\d.,]+\s*(?:mil)?)\s*OFF\b", re.I)
_MINIMUM = re.compile(r"(?:acima de|a partir de)\s*R\$\s*([\d.,]+\s*(?:mil)?)", re.I)
_MAXIMUM = re.compile(r"limitado a\s*R\$\s*([\d.,]+\s*(?:mil)?)", re.I)
_EXPECTED_REJECTIONS = {"unavailable", "cashback_not_discount", "duplicate"}
_VOUCHER_API_PATH = "/api/v1/microsite/get_vouchers_by_collections"
_SHOPEE_MONEY_SCALE = 100_000


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


def _browser_context_options():
    server = str(
        getattr(settings, "SHOPEE_PUBLIC_PROXY_SERVER", "") or ""
    ).strip()
    if not server:
        return {}
    if not re.match(r"^(?:https?|socks5)://", server, re.I):
        raise ValueError("SHOPEE_PUBLIC_PROXY_SERVER precisa incluir o protocolo")
    proxy = {"server": server}
    username = str(
        getattr(settings, "SHOPEE_PUBLIC_PROXY_USERNAME", "") or ""
    ).strip()
    password = str(
        getattr(settings, "SHOPEE_PUBLIC_PROXY_PASSWORD", "") or ""
    ).strip()
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    return {"proxy": proxy}


def _economizar_banda(page):
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media", "font"}
        else route.continue_(),
    )


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


def _parse_api_voucher(raw, *, now_ts=None):
    """Normaliza o contrato JSON que hidrata os cards oficiais.

    A Shopee representa dinheiro em centésimos de milésimo de real (R$ 1 =
    100.000) e separa desconto imediato (``reward_type=0``) de cashback em moedas.
    O JSON também expõe validade e quota; por isso ele prevalece sobre o texto do
    DOM quando ambos estão disponíveis.
    """
    voucher = (raw or {}).get("voucher") if isinstance(raw, dict) else None
    voucher = voucher if isinstance(voucher, dict) else raw
    if not isinstance(voucher, dict):
        return None, "invalid_api_row"

    identifier = voucher.get("voucher_identifier") or {}
    reward = voucher.get("reward_info") or {}
    info = voucher.get("info") or {}
    timing = voucher.get("time_info") or {}
    quota = voucher.get("quota_info") or {}
    ui = voucher.get("ui_info") or {}
    try:
        promotion_id = str(int(identifier.get("promotion_id") or 0))
        signature_source = int(identifier.get("signature_source") or 0)
        status = int(info.get("status") or 0)
        start_ts = int(timing.get("start_time") or 0)
        end_ts = int(timing.get("end_time") or 0)
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_api_identity"
    signature = str(identifier.get("signature") or "").strip()
    voucher_code = str(identifier.get("voucher_code") or "").strip()
    if (
        promotion_id == "0" or not voucher_code.isdigit()
        or not re.fullmatch(r"[a-f0-9]{64}", signature, re.I)
    ):
        return None, "invalid_api_identity"

    now_ts = int(time.time() if now_ts is None else now_ts)
    unavailable = (
        status != 1
        or bool(timing.get("has_expired"))
        or (start_ts and start_ts > now_ts)
        or (end_ts and end_ts < now_ts)
        or bool(quota.get("fully_redeemed"))
        or bool(quota.get("fully_used"))
        or bool(quota.get("disabled"))
    )
    if unavailable:
        return None, "unavailable"
    try:
        reward_type = int(reward.get("reward_type") or 0)
    except (TypeError, ValueError):
        return None, "invalid_api_reward"
    if reward_type != 0:
        return None, "cashback_not_discount"

    try:
        percentage = float(reward.get("percentage") or 0)
        fixed = float(reward.get("value") or 0) / _SHOPEE_MONEY_SCALE
        minimum = float(reward.get("min_spend") or 0) / _SHOPEE_MONEY_SCALE
        cap = float(reward.get("cap") or 0) / _SHOPEE_MONEY_SCALE
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_api_reward"
    if percentage > 0:
        discount_type, discount = "porcentagem", percentage
        maximum = cap or None
        if discount >= 100:
            return None, "implausible_discount"
    elif fixed > 0:
        discount_type, discount, maximum = "fixo", fixed, None
    else:
        return None, "missing_discount"

    category = str(ui.get("icon_text") or "Todas as lojas").strip()
    if not category:
        category = "Todas as lojas"
    evcode = base64.b64encode(voucher_code.encode("ascii")).decode("ascii")
    query = urlencode({
        "evcode": evcode,
        "promotionId": promotion_id,
        "signature": signature,
        "source": signature_source,
    })
    label = (
        f"{discount:g}% OFF" if discount_type == "porcentagem"
        else f"R$ {discount:g} OFF"
    )
    starts_at = (
        datetime.fromtimestamp(start_ts, tz=datetime_timezone.utc)
        if start_ts else None
    )
    # 2147483640/2147483647 é o sentinela da plataforma para campanha sem fim
    # materializado. Exibi-lo como "válido até 2038" seria uma promessa enganosa;
    # a remoção é controlada pelo snapshot completo da fonte.
    valid_until = (
        datetime.fromtimestamp(end_ts, tz=datetime_timezone.utc)
        if end_ts and end_ts < 2_147_400_000 else None
    )
    return {
        "promotion_id": promotion_id,
        "title": f"Cupom Shopee - {label} - {category}"[:255],
        "url": f"https://shopee.com.br/voucher/details?{query}",
        "category": category[:100],
        "discount_type": discount_type,
        "discount": round(discount, 2),
        "minimum": round(minimum, 2) if minimum else None,
        "maximum": round(maximum, 2) if maximum else None,
        "restricted": bool(
            voucher.get("exclusive_channel_type")
            or ui.get("user_scope_error_message")
        ),
        "text": (
            f"status={info.get('status')}; reward_type={reward_type}; "
            f"claimed={quota.get('percentage_claimed')}; "
            f"used={quota.get('percentage_used')}"
        )[:1000],
        "image": "",
        "starts_at": starts_at,
        "valid_until": valid_until,
    }, ""


def _api_voucher_entries(payloads):
    """Extrai as linhas somente de respostas completas e sem erro da Shopee."""
    entries = []
    contract_seen = False
    complete = True
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get("error") not in (None, 0):
            continue
        collections = payload.get("data")
        if not isinstance(collections, list):
            continue
        contract_seen = True
        for collection in collections:
            if isinstance(collection, dict):
                rows = [
                    row for row in (collection.get("vouchers") or [])
                    if isinstance(row, dict)
                ]
                entries.extend(rows)
                try:
                    total = int(collection.get("total_count") or len(rows))
                except (TypeError, ValueError):
                    complete = False
                else:
                    if total > len(rows):
                        complete = False
    return entries, contract_seen, complete


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
    # Vouchers gerais e cupons diarios de vendedores vivem em colecoes oficiais
    # diferentes. Ambos passam pelo mesmo contrato assinado e pelos mesmos gates.
    coupon_pages = COUPON_PAGES

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
                    "pages_processed": 0, "pages": [],
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
                    "pages_processed": 0, "pages": [],
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
                self.last_health_status = "auth_required"
                return

        refreshed_state = None
        auth_required = False
        api_responses = []
        page_snapshots = []
        seen = set()
        with iniciar_browser(
            storage_state=state, headless=True, **_browser_context_options(),
        ) as (page, context):
            _economizar_banda(page)
            page.on(
                "response",
                lambda response: api_responses.append(response)
                if _VOUCHER_API_PATH in response.url else None,
            )
            for coupons_url in self.coupon_pages:
                response_start = len(api_responses)
                page.goto(
                    coupons_url, wait_until="domcontentloaded", timeout=45000,
                )
                # O HTML inicial contem apenas o shell; os vouchers chegam no
                # hydrate. Esperar o contrato evita um falso erro de schema.
                try:
                    page.wait_for_selector(
                        "a[href*='/voucher/details']", timeout=12000,
                    )
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
                body = page.locator("body").inner_text(timeout=10000)
                folded = body.casefold()
                page_auth_required = _auth_required(page.url, body)
                if page_auth_required:
                    auth_required = True
                    capture_public_diagnostic(page, self.slug, "auth_required")
                    page_snapshots.append({
                        "url": coupons_url, "items_seen": 0, "accepted": 0,
                        "complete": False, "health": "auth_required",
                        "schema_errors": 0,
                    })
                    break
                if any(marker in folded for marker in (
                        "verifique que voce e humano",
                        "verifique que você é humano",
                        "access denied", "captcha")):
                    capture_public_diagnostic(page, self.slug, "captcha_or_block")
                    self.last_health_status = "blocked"
                    raise RuntimeError("captcha")

                api_payloads = []
                for response in api_responses[response_start:]:
                    try:
                        if response.status == 200:
                            api_payloads.append(response.json())
                    except Exception:
                        continue
                api_cards, api_contract_seen, api_complete = _api_voucher_entries(
                    api_payloads,
                )
                cards = api_cards if api_contract_seen else None
                # Classes CSS sao ofuscadas; o fallback procura a menor caixa que
                # contenha simultaneamente desconto, estado e link dos termos.
                if cards is None:
                    cards = page.locator(
                        "a[href*='/voucher/details']",
                    ).evaluate_all("""
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

                page_rejected = defaultdict(int)
                accepted_before = len(accepted)
                for raw in cards:
                    row, reason = (
                        _parse_api_voucher(raw)
                        if api_contract_seen else _parse_rendered_card(raw)
                    )
                    if row is None:
                        reason = reason or "invalid"
                        rejected[reason] += 1
                        page_rejected[reason] += 1
                        continue
                    if row["promotion_id"] in seen:
                        rejected["duplicate"] += 1
                        page_rejected["duplicate"] += 1
                        continue
                    seen.add(row["promotion_id"])
                    if not api_contract_seen:
                        row["image"] = str(
                            raw.get("image") or ""
                        ).split("?", 1)[0][:1000]
                    row["source_page"] = coupons_url
                    accepted.append(row)

                accepted_page = len(accepted) - accepted_before
                page_complete, page_health, page_schema_errors = _snapshot_state(
                    len(cards), accepted_page, page_rejected,
                )
                if api_contract_seen and not api_complete:
                    page_complete = False
                    page_health = "partial" if accepted_page else "degraded"
                    page_schema_errors += 1
                page_snapshots.append({
                    "url": coupons_url, "items_seen": len(cards),
                    "accepted": accepted_page, "complete": page_complete,
                    "health": page_health,
                    "schema_errors": page_schema_errors,
                })
                if not cards:
                    capture_public_diagnostic(
                        page, self.slug, "voucher_cards_not_found",
                    )
            if usuario is not None and not auth_required:
                refreshed_state = context.storage_state()

        if refreshed_state is not None:
            from apps.scrapers.report_sessions import save_report_state
            save_report_state(usuario, "shopee_shop", refreshed_state)

        complete = bool(
            len(page_snapshots) == len(self.coupon_pages)
            and all(row["complete"] for row in page_snapshots)
        )
        schema_errors = sum(row["schema_errors"] for row in page_snapshots)
        if auth_required:
            complete, health, schema_errors = False, "auth_required", 0
        elif accepted:
            health = "healthy" if complete else "partial"
        else:
            health = "healthy_empty" if complete else "degraded"
        items_seen = sum(row["items_seen"] for row in page_snapshots)
        self.last_metrics = {
            "items_seen": items_seen,
            "accepted": len(accepted),
            "rejected": sum(rejected.values()),
            "rejected_by_reason": dict(sorted(rejected.items())),
            "complete": complete,
            "reason_code": "auth_required" if auth_required else "",
            "schema_errors": schema_errors,
            "pages_processed": len(page_snapshots),
            "pages": page_snapshots,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "schema_fingerprint": hashlib.sha256(
                "voucher-details|discount|state|multi-page-v2".encode("utf-8")
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
                starts_at=row.get("starts_at"),
                valid_until=row.get("valid_until"),
                evidence={
                    "transport": "shopee-official-coupon-page",
                    "association": "shopee-official-coupon-page",
                    "promotion_id": row["promotion_id"],
                    "availability": "claimable",
                    "source_page": row.get("source_page") or COUPONS_URL,
                    "snapshot": row["text"],
                },
            )

    def healthcheck(self):
        try:
            return {"ok": bool(list(self.discover_coupons()))}
        except Exception as exc:
            return {"ok": False, "erro": "Falha temporaria na fonte publica.",
                    "cause": type(exc).__name__}
