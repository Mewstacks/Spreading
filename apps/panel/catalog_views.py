"""Ofertas e Cupons: o catálogo que o motor enxerga, com busca, filtros e envio manual."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.compose.notice import brl, coupon_line, pct
from apps.coupons.models import CampaignCoupon, Coupon
from apps.delivery.outbox import deliver_due, enqueue_specific
from apps.delivery.transports import SessionRouter, WahaTransport
from apps.offers.models import STORE_LABELS, AffiliateLink, Offer, Rule

from .queries import live_coupons, live_offers, with_link
from .views import REASONS, current_org

PAGE = 30
CAMPAIGN_MAX_AGE = timedelta(hours=6)
EXTRA_REASONS = {
    "grupo_indisponivel": "Grupo ocupado agora", "cupom_vencido": "Cupom vencido",
    "observacao_velha": "Observação velha",
}


def _filtered(request, org, kind, now):
    qs = live_coupons(now) if kind == "coupon" else live_offers(now)
    query = (request.GET.get("q") or "").strip()
    if query:
        qs = qs.filter(Q(title__icontains=query) | Q(coupon_code__icontains=query)
                       | Q(product_key__icontains=query))
    store = request.GET.get("loja", "")
    if store in STORE_LABELS:
        qs = qs.filter(store=store)
    state = request.GET.get("estado", "")
    if state == "com":
        qs = qs.filter(with_link(org))
    elif state == "sem":
        qs = qs.exclude(with_link(org))
    return qs.order_by("-discount_pct", "-last_seen")


def _coupon_text(offer) -> str:
    if not offer.coupon_kind:
        return ""
    head = offer.coupon_code if offer.coupon_kind == "code" else "Ativar"
    value = f"{pct(offer.coupon_pct)} OFF" if offer.coupon_pct else (
        f"{brl(offer.coupon_amount)} OFF" if offer.coupon_amount else "")
    return " · ".join(p for p in (head, value) if p)


@staff_member_required
def catalog(request, kind="all"):
    org = current_org(request)
    now = timezone.now()
    qs = _filtered(request, org, kind, now)
    page = max(1, int(request.GET["p"])) if request.GET.get("p", "").isdigit() else 1
    chunk = list(qs[(page - 1) * PAGE:page * PAGE + 1])
    offers = chunk[:PAGE]
    have = set(AffiliateLink.objects.for_org(org)
               .filter(product_key__in={k for o in offers for k in (o.link_key, o.product_key)})
               .values_list("product_key", flat=True))
    rows = [{
        "offer": o, "store": STORE_LABELS.get(o.store, o.store),
        "coupon": _coupon_text(o), "price": brl(o.price_to), "pct": f"{round(o.discount_pct)}%",
        "linked": o.link_key in have or (o.store == "amazon" and bool(org.amazon_tag))
        or (o.store == "mercadolivre" and o.coupon_kind == "code" and o.product_key in have),
        "until": o.coupon_until.astimezone(timezone.get_current_timezone()).strftime("%d/%m")
        if o.coupon_until else "",
    } for o in offers]
    rules = list(Rule.objects.for_org(org).filter(type=Rule.OFFERS)
                 .select_related("destination").order_by("destination__name"))
    feed, campaigns = [], []
    if kind == "coupon":
        today = timezone.localdate()
        live = Coupon.objects.filter(valid_until__gte=today).order_by("valid_until")[:150]
        feed = [{"line": coupon_line(c).replace("*", ""), "until": c.valid_until} for c in live]
        # Campanha que saiu da vitrine (não vista há 6 h) ou venceu não aparece como se valesse.
        shown = CampaignCoupon.objects.filter(
            expires_at__gt=now, last_seen__gte=now - CAMPAIGN_MAX_AGE).order_by("expires_at")[:60]
        campaigns = [{
            "title": c.title, "category": c.category, "items": c.total_items,
            "min": brl(c.min_purchase) if c.min_purchase else "",
            "cap": brl(c.max_discount) if c.max_discount else "",
            "until": c.expires_at.astimezone(timezone.get_current_timezone()).strftime("%d/%m"),
            "url": c.container_url,
        } for c in shown]
    params = request.GET.copy()
    params.pop("p", None)
    return render(request, "panel/catalog.html", {
        "kind": kind, "title": "Cupons" if kind == "coupon" else "Ofertas", "rows": rows,
        "q": request.GET.get("q", ""), "store": request.GET.get("loja", ""),
        "state": request.GET.get("estado", ""), "stores": STORE_LABELS, "rules": rules,
        "page": page, "has_next": len(chunk) > PAGE, "has_prev": page > 1,
        "base_query": params.urlencode(), "feed": feed, "campaigns": campaigns,
        "next": request.get_full_path(),
    })


@staff_member_required
@require_POST
def send_offer(request, offer_id):
    org = current_org(request)
    offer = get_object_or_404(Offer, id=offer_id)
    try:
        rule = Rule.objects.for_org(org).select_related("destination").get(
            id=int(request.POST.get("rule", "")), type=Rule.OFFERS)
    except (Rule.DoesNotExist, ValueError):
        raise Http404 from None
    item, reason = enqueue_specific(org, rule, offer)
    if item is None:
        label = {**REASONS, **EXTRA_REASONS}.get(reason, reason)
        messages.error(request, f"Não saiu: {label}.")
    elif deliver_due(SessionRouter(factory=WahaTransport))["confirmado"]:
        messages.success(request, "Enviado.")
    else:
        messages.error(request, "Na fila; envio não confirmado.")
    target = request.POST.get("next", "")
    safe = target.startswith("/painel/") and "//" not in target
    return redirect(target if safe else "panel:offers")
