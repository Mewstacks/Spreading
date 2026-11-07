"""Feed de cupons da equipe de afiliados do ML (página estática com `const COUPONS = [...]`)."""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Coupon

BRT = ZoneInfo("America/Sao_Paulo")
SOURCE = "ml-feed-afiliados"


@dataclass(frozen=True)
class CouponData:
    code: str
    discount_pct: Decimal | None
    discount_amount: Decimal | None
    min_purchase: Decimal | None
    max_discount: Decimal | None
    starts_on: date | None
    valid_until: date
    container_url: str
    scope: str


def parse_br_number(text) -> Decimal | None:
    """'1.199' -> 1199 ; '129.90' -> 129.90 ; '30,00%' -> 30.00 ; 'R$ 1.199,90' -> 1199.90."""
    raw = re.sub(r"[^\d.,-]", "", str(text or ""))
    if not raw or raw in {"-", ".", ","}:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    elif "." in raw and len(raw.rsplit(".", 1)[1]) == 3:
        raw = raw.replace(".", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _balanced(html: str, start: int) -> str | None:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return html[start : i + 1]
    return None


def extract_array(html: str, var_name: str = "COUPONS") -> list[dict]:
    match = re.search(rf"\b{re.escape(var_name)}\s*=\s*\[", html)
    if not match:
        return []
    chunk = _balanced(html, match.end() - 1)
    if not chunk:
        return []
    try:
        data = json.loads(chunk)
    except json.JSONDecodeError:
        return []
    return [row for row in data if isinstance(row, dict)]


def _parse_date(text) -> date | None:
    try:
        return datetime.strptime(str(text).strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,39}$")
ALLOWED_HOST_SUFFIX = ".mercadolivre.com.br"


def safe_container_url(url: str) -> str:
    """Só https em *.mercadolivre.com.br; qualquer outra coisa vira vazio."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host.endswith(ALLOWED_HOST_SUFFIX):
        return ""
    return url.strip()


def normalize(row: dict, today: date) -> CouponData | None:
    code = str(row.get("nome") or "").strip().upper()
    valid_until = _parse_date(row.get("dia_fim"))
    starts_on = _parse_date(row.get("dia_inicio"))
    if not CODE_RE.match(code) or not valid_until or valid_until < today:
        return None
    if starts_on and starts_on > today:
        return None
    value_text = str(row.get("valor_desconto") or "")
    number = parse_br_number(value_text)
    is_amount = "R$" in value_text.upper()
    container = safe_container_url(str(row.get("container_url") or ""))
    site_wide = bool(row.get("is_mar_aberto"))
    return CouponData(
        code=code,
        discount_pct=None if is_amount else number,
        discount_amount=number if is_amount else None,
        min_purchase=parse_br_number(row.get("min_compra")),
        max_discount=parse_br_number(row.get("desconto_max")),
        starts_on=starts_on,
        valid_until=valid_until,
        container_url=container,
        scope=Coupon.SITE if site_wide else (Coupon.CONTAINER if container else Coupon.PRODUCTS),
    )


def parse_feed(html: str, today: date) -> list[CouponData]:
    seen, out = set(), []
    for row in extract_array(html):
        item = normalize(row, today)
        if item and (item.code, item.valid_until) not in seen:
            seen.add((item.code, item.valid_until))
            out.append(item)
    return out


def fetch_feed(url: str | None = None, session=None) -> str:
    http = session or requests
    response = http.get(
        url or settings.ML_COUPON_FEED_URL,
        timeout=settings.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": "Mozilla/5.0 (compatible; SpreadingBot/2.0)"},
    )
    response.raise_for_status()
    return response.text


@transaction.atomic
def ingest(html: str, now: datetime | None = None) -> int:
    """Grava/atualiza cupons e carimba `last_seen`. Devolve quantos estão no feed agora."""
    now = now or timezone.now()
    today = now.astimezone(BRT).date()
    count = 0
    for item in parse_feed(html, today):
        Coupon.objects.update_or_create(
            store="mercadolivre",
            code=item.code,
            valid_until=item.valid_until,
            defaults={
                "source": SOURCE,
                "discount_pct": item.discount_pct,
                "discount_amount": item.discount_amount,
                "min_purchase": item.min_purchase,
                "max_discount": item.max_discount,
                "starts_on": item.starts_on,
                "container_url": item.container_url,
                "scope": item.scope,
                "last_seen": now,
            },
        )
        count += 1
    return count
