"""Manual: o dono cola links de produto (e, se quiser, um cupom do ML) e a oferta entra."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone

from apps.compose.notice import brl
from apps.offers import manual
from apps.offers.models import STORE_LABELS, Rule

from .views import current_org

MAX_TEXT = 20000
FIELDS = ("code", "tipo", "valor", "minimo", "teto", "validade")


def _number(raw: str) -> Decimal | None:
    """`20`, `20,5` e `1.234,56` viram Decimal; vazio dá None; lixo levanta ValueError."""
    raw = (raw or "").strip().replace(" ", "").removeprefix("R$")
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(raw) from exc
    if not value.is_finite() or value < 0 or value >= Decimal(10) ** 7:
        raise ValueError(raw)
    return value


def read_coupon(post) -> tuple[manual.Coupon | None, str]:
    """(cupom, erro): qualquer campo do cupom preenchido pede o valor e a validade."""
    if not any((post.get(name) or "").strip() for name in ("code", "valor", "minimo", "teto",
                                                          "validade")):
        return None, ""
    try:
        value, minimum, cap = (_number(post.get(n)) for n in ("valor", "minimo", "teto"))
    except ValueError:
        return None, "Número inválido."
    if not value:
        return None, "Falta o valor do cupom."
    try:
        until = date.fromisoformat((post.get("validade") or "").strip())
    except ValueError:
        return None, "Falta a validade do cupom."
    kind = "amount" if post.get("tipo") == "amount" else "pct"
    return manual.Coupon(
        code=(post.get("code") or "").strip(), kind=kind, value=value, minimum=minimum, cap=cap,
        until=until), ""


def _row(outcome) -> dict:
    return {
        "url": outcome.url, "status": outcome.status, "offer": outcome.offer,
        "title": outcome.title, "store": STORE_LABELS.get(outcome.store, ""),
        "price": brl(outcome.price) if outcome.price else "",
        "reason": manual.REASONS.get(outcome.reason, outcome.reason),
    }


@staff_member_required
def page(request):
    org = current_org(request)
    form = {"links": "", **{name: "" for name in FIELDS}, "tipo": "pct"}
    results = []
    if request.method == "POST":
        form.update({name: (request.POST.get(name) or "").strip() for name in FIELDS})
        form["links"] = (request.POST.get("links") or "")[:MAX_TEXT]
        coupon, error = read_coupon(request.POST)
        if error:
            messages.error(request, error)
        elif not manual.parse(form["links"]):
            messages.error(request, "Cole ao menos um link.")
        else:
            report = manual.add(org, form["links"], coupon, now=timezone.now())
            results = [_row(o) for o in report.outcomes]
            if report.ignored:
                messages.warning(
                    request, f"{report.ignored} linhas ficaram de fora (máx. {manual.MAX_LINES}).")
    rules = list(Rule.objects.for_org(org).filter(type=Rule.OFFERS)
                 .select_related("destination").order_by("destination__name"))
    return render(request, "panel/manual.html", {
        "form": form, "results": results, "rules": rules, "next": request.path,
        "has_coupon": any(form[name] for name in ("code", "valor", "minimo", "teto", "validade")),
    })
