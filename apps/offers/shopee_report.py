"""Vendas da Shopee (relatório de conversões da API de afiliados): receita e comissão no painel.

`conversionReport` devolve um nó por pedido atribuído à conta, com status (PENDING, COMPLETED,
CANCELLED), comissão líquida e a origem do clique. O schema foi lido por introspecção na API real em
08/11/2026. Uma carga por hora, janela de 60 dias (90 na primeira vez): o status de um pedido muda
até semanas depois, então a janela inteira é regravada a cada carga. Falha da API sobe como exceção
para a tela Fontes e espera uma hora.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone as dj_timezone

from apps.orgs.models import Organization

from . import shopee
from .catalog import REFRESH_EVERY
from .models import Conversion, Rule, TermRun

RUN_KEY = "shopee:vendas"
STORE = "shopee"
WINDOW_DAYS, FIRST_WINDOW_DAYS = 60, 90
PAGE_SIZE, MAX_PAGES = 100, 30
SCROLL = re.compile(r"^[A-Za-z0-9+/=_-]{1,200}$")  # o cursor volta na próxima consulta: só base64
QUERY = """
query {
  conversionReport(
    purchaseTimeStart: %(start)d, purchaseTimeEnd: %(end)d, limit: %(limit)d%(scroll)s
  ) {
    nodes {
      conversionId purchaseTime conversionStatus netCommission utmContent referrer
      orders { orderId items { itemName qty actualAmount } }
    }
    pageInfo { hasNextPage scrollId }
  }
}
"""
MONEY_LIMIT = Decimal(10) ** 8


def _dec(value, places: int) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0 or number >= MONEY_LIMIT:
        return None
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _row(node, org, now):
    """Linha de um nó do relatório, ou None se o nó não presta (nunca derruba a carga)."""
    try:
        orders = [o for o in node["orders"] if isinstance(o, dict)]
        order = orders[0]
        items = [i for o in orders for i in o["items"] if isinstance(i, dict)]
        commission = _dec(node["netCommission"], 4)
        amount = sum((_dec(i.get("actualAmount") or 0, 2) or Decimal(0) for i in items),
                     Decimal(0))
        purchased = datetime.fromtimestamp(int(node["purchaseTime"]), tz=timezone.utc)
        external = f"{node['conversionId']}:{order['orderId']}"[:80]
    except (KeyError, IndexError, TypeError, ValueError, OverflowError, OSError):
        return None
    if commission is None or not items or amount >= MONEY_LIMIT:
        return None
    return Conversion(
        org=org, store=STORE, external_id=external, purchased_at=purchased,
        status=str(node.get("conversionStatus") or "")[:20], title=str(items[0].get("itemName")
                                                                       or "").strip()[:300],
        items=min(len(items), 32000), amount=amount, commission=commission,
        channel=str(node.get("referrer") or "")[:40], sub_id=str(node.get("utmContent") or "")[:80],
        synced_at=now,
    )


def _owner():
    """Org dona dos links da Shopee: a de uma regra ativa com Shopee, senão a primeira."""
    rule = (Rule.unscoped.filter(active=True, stores__contains=STORE).select_related("org")
            .order_by("id").first())
    return rule.org if rule else Organization.objects.order_by("id").first()


def _fetch(start, end, http):
    scroll = ""
    for _page in range(MAX_PAGES):
        extra = f", scrollId: {json.dumps(scroll)}" if scroll else ""
        data = shopee.execute(
            QUERY % {"start": start, "end": end, "limit": PAGE_SIZE, "scroll": extra}, None,
            app_id=settings.SHOPEE_APP_ID, secret=settings.SHOPEE_APP_SECRET, http=http)
        report = data.get("conversionReport") or {}
        yield from (n for n in report.get("nodes") or [] if isinstance(n, dict))
        info = report.get("pageInfo") or {}
        scroll = str(info.get("scrollId") or "")
        if not info.get("hasNextPage") or not SCROLL.match(scroll):
            return


def sync(now=None, http=None) -> dict:
    """Grava as vendas da janela; {} se não há nada a fazer agora."""
    if not shopee.configured():
        return {}
    now = now or dj_timezone.now()
    last = TermRun.objects.filter(term=RUN_KEY).values_list("last_run", flat=True).first()
    if last and now - last < REFRESH_EVERY:
        return {}
    org = _owner()
    if org is None:
        return {}
    TermRun.objects.update_or_create(term=RUN_KEY, defaults={"last_run": now})  # falha espera 1 h
    first = not Conversion.unscoped.filter(org=org, store=STORE).exists()
    start = now - timedelta(days=FIRST_WINDOW_DAYS if first else WINDOW_DAYS)
    rows = {}
    for node in _fetch(int(start.timestamp()), int(now.timestamp()), http):
        row = _row(node, org, now)
        if row:
            rows[row.external_id] = row
    if rows:
        Conversion.unscoped.bulk_create(
            list(rows.values()), update_conflicts=True,
            unique_fields=["org", "store", "external_id"],
            update_fields=["purchased_at", "status", "title", "items", "amount", "commission",
                           "channel", "sub_id", "synced_at"])
    return {"vendas": len(rows)}
