"""Receita: vendas e comissão que a loja atribui ao dono (só a Shopee tem API de relatório)."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.shortcuts import render
from django.utils import timezone

from apps.coupons.feed import BRT
from apps.offers import shopee
from apps.offers.models import Conversion
from apps.orgs.models import Destination

from .views import current_org

PERIODS = (7, 30, 60)
DEFAULT_DAYS = 30
LATEST = 20
SUB_ID = re.compile(r"^spreading-g(\d+)(?:-|$)")  # sub-ids do link que o Spreading manda
STATUS = {  # texto da loja -> (rótulo, tom do selo)
    "COMPLETED": ("Confirmada", "green"),
    "PENDING": ("Pendente", "yellow"),
    "CANCELLED": ("Cancelada", "red"),
}


def money(value) -> str:
    """R$ 1.234,56 (sempre com centavos: as colunas alinham)."""
    text = f"{Decimal(value or 0).quantize(Decimal('0.01')):,.2f}"
    return "R$ " + text.replace(",", "_").replace(".", ",").replace("_", ".")


def _days(request) -> int:
    raw = request.GET.get("dias", "")
    return int(raw) if raw.isdigit() and int(raw) in PERIODS else DEFAULT_DAYS


def _groups(org, attributed) -> list[dict]:
    """Vendas dos links com sub-id do Spreading, por grupo (maior comissão primeiro)."""
    found = {}
    for row in attributed.values("sub_id").annotate(n=Count("id"), total=Sum("commission")):
        match = SUB_ID.match(row["sub_id"])
        if match:
            count, total = found.setdefault(int(match.group(1)), [0, Decimal(0)])
            found[int(match.group(1))] = [count + row["n"], total + (row["total"] or 0)]
    names = dict(Destination.objects.for_org(org).filter(id__in=found).values_list("id", "name"))
    ordered = sorted(found.items(), key=lambda item: -item[1][1])
    return [{"name": names.get(ident, f"Grupo {ident}"), "count": count,
             "commission": money(total)} for ident, (count, total) in ordered]


@staff_member_required
def revenue(request):
    org = current_org(request)
    days = _days(request)
    sales = Conversion.objects.for_org(org).filter(
        store="shopee", purchased_at__gte=timezone.now() - timedelta(days=days))
    live = ~Q(status="CANCELLED")
    total = sales.aggregate(
        done=Sum("commission", filter=Q(status="COMPLETED")),
        done_n=Count("id", filter=Q(status="COMPLETED")),
        pending=Sum("commission", filter=Q(status="PENDING")),
        pending_n=Count("id", filter=Q(status="PENDING")),
        cancelled_n=Count("id", filter=Q(status="CANCELLED")),
        sold=Sum("amount", filter=live), items=Sum("items", filter=live),
    )
    channels = [{
        "name": row["channel"] or "—", "count": row["n"], "commission": money(row["total"]),
    } for row in sales.filter(live).values("channel").annotate(
        n=Count("id"), total=Sum("commission")).order_by("-total")]
    latest = []
    for sale in sales.order_by("-purchased_at")[:LATEST]:
        label, tone = STATUS.get(sale.status, (sale.status.title() or "—", "muted"))
        latest.append({
            "when": sale.purchased_at.astimezone(BRT).strftime("%d/%m %H:%M"),
            "title": sale.title or "—", "more": max(0, sale.items - 1),
            "channel": sale.channel or "—", "label": label, "tone": tone,
            "commission": money(sale.commission),
        })
    return render(request, "panel/revenue.html", {
        "groups": _groups(org, sales.filter(live, sub_id__startswith="spreading-")),
        "enabled": shopee.configured(), "days": days, "periods": PERIODS,
        "done": money(total["done"]), "done_n": total["done_n"],
        "pending": money(total["pending"]), "pending_n": total["pending_n"],
        "sold": money(total["sold"]), "items": total["items"] or 0,
        "cancelled_n": total["cancelled_n"], "channels": channels, "latest": latest,
        "empty": not latest,
    })
