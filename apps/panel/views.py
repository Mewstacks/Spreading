"""Painel enxuto: escolher, por grupo, nichos, lojas e cadência."""
import re
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.coupons.feed import BRT
from apps.delivery.models import OutboxItem
from apps.delivery.outbox import deliver_due, enqueue_for_rule, explain_rule
from apps.delivery.transports import WahaTransport
from apps.linkbuilder import manual
from apps.linkbuilder.models import BuilderState
from apps.offers import niches
from apps.offers.models import STORE_LABELS, STORES, Rule
from apps.orgs.models import Destination, Organization

from .queries import live_coupons, with_link

REASONS = {
    "prontas": "Prontas para sair",
    "sem_link_do_dono": "Sem link seu",
    "sem_prova_de_preco": "Sem prova de preço",
    "fora_do_nicho": "Fora do nicho",
    "cooldown": "Já enviadas",
    "fora_da_faixa_de_preco": "Fora da faixa de preço",
    "feed_velho": "Feed velho",
    "regra_ou_grupo_fora_da_janela": "Fora da janela ou no intervalo",
    "sem_foto": "Sem foto",
    "desconto_caiu": "Desconto caiu",
    "desconto_suspeito": "Desconto suspeito",
    "preco_inconclusivo": "Preço inconclusivo",
    "preco_diverge_da_prova": "Preço mudou",
    "sem_desconto": "Sem desconto",
    "ja_enfileirada": "Já na fila",
}


BUILDER_LABELS = {
    "pronto": "Pronto", "ocioso": "Ocioso", "desligado": "Desligado", "sem_sessao": "Sem sessão",
    "login": "Entrar de novo", "verificacao": "Verificação", "indisponivel": "Fora do ar",
}
WEEKDAYS = {"1": "Seg", "2": "Ter", "3": "Qua", "4": "Qui", "5": "Sex", "6": "Sáb", "7": "Dom"}
BUILDER_FINE = {"pronto", "ocioso"}
WHATSAPP_LABELS = {"WORKING": "Conectado", "SCAN_QR_CODE": "Ler o QR", "STARTING": "Iniciando",
                   "PASSKEY_REQUIRED": "Passkey", "PASSKEY_CONFIRMATION_REQUIRED": "Passkey",
                   "FAILED": "Falhou", "STOPPED": "Parado", "": "Sem resposta"}


def _whatsapp_status():
    status = WahaTransport().session_status(timeout=3)
    return {"label": WHATSAPP_LABELS.get(status, status),
            "tone": "green" if status == "WORKING" else "yellow"}


def _builder_status(org):
    state = BuilderState.objects.for_org(org).first()
    if state is None:
        return None
    return {
        "label": BUILDER_LABELS.get(state.state, state.state), "detail": state.detail,
        "tone": "green" if state.state in BUILDER_FINE else (
            "muted" if state.state == BuilderState.OFF else "yellow"),
    }


def _offer_name(body: str) -> str:
    """Nome do produto na primeira linha da legenda (`🛍️ *Nome*`)."""
    match = re.search(r"\*(.+?)\*", (body or "").split("\n", 1)[0])
    return match.group(1) if match else (body or "")[:60]


def niche_label(key: str) -> str:
    body = key.split(":", 1)[-1]
    return body.split("|", 1)[-1]


def current_org(request):
    org = Organization.objects.order_by("id").first()
    if org is None:
        raise Http404
    return org


def _today_start(now):
    return now.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0)


@staff_member_required
def groups(request):
    org = current_org(request)
    now = timezone.now()
    cards = []
    rules = Rule.objects.for_org(org).filter(type=Rule.OFFERS).select_related("destination")
    for rule in rules.order_by("destination__name"):
        sent = OutboxItem.objects.for_org(org).filter(
            rule=rule, state=OutboxItem.CONFIRMED, created_at__gte=_today_start(now)
        ).count()
        picked = [niche_label(k) for k in rule.niches or []]
        cards.append({
            "rule": rule, "sent_today": sent, "cap": rule.effective_cap,
            "niches": picked[:2], "more": max(0, len(picked) - 2), "all": not picked,
        })
    coupons = live_coupons(now)
    linked = coupons.filter(with_link(org)).count()
    stats = {
        "sent": sum(c["sent_today"] for c in cards), "cap": sum(c["cap"] for c in cards),
        "active": sum(1 for c in cards if c["rule"].active), "total": len(cards),
        "linked": linked, "unlinked": coupons.count() - linked,
    }
    return render(request, "panel/groups.html", {
        "org": org, "cards": cards, "builder": _builder_status(org), "stats": stats,
        "whatsapp": _whatsapp_status(),
    })


@staff_member_required
def add_group(request):
    org = current_org(request)
    transport = WahaTransport()
    available = transport.list_groups()
    taken = set(
        Destination.objects.for_org(org).values_list("external_id", flat=True)
    )
    if request.method == "POST":
        chosen = next((g for g in available if g["id"] == request.POST.get("group")), None)
        if chosen is None or chosen["id"] in taken:
            messages.error(request, "Grupo indisponível.")
            return redirect("panel:add")
        dest = Destination.unscoped.create(
            org=org, kind=Destination.WA_GROUP, external_id=chosen["id"], name=chosen["name"]
        )
        rule = Rule.unscoped.create(org=org, destination=dest, name=chosen["name"], active=False)
        messages.success(request, "Grupo adicionado.")
        return redirect("panel:group", rule_id=rule.id)
    query = (request.GET.get("q") or "").strip().lower()
    rows = [g for g in available if g["id"] not in taken and query in g["name"].lower()]
    return render(request, "panel/add_group.html", {
        "org": org, "rows": rows[:100], "q": request.GET.get("q", ""),
        "offline": not available,
    })


def _int(value, lo, hi, default):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _money(value):
    try:
        number = Decimal(str(value or "").strip().replace(",", "."))
    except InvalidOperation:
        return None
    return number.quantize(Decimal("0.01")) if 0 < number < Decimal("10000000") else None


def _terms(value) -> str:
    return ", ".join(t.strip() for t in str(value or "").split(",") if t.strip())[:500]


def _get_rule(org, rule_id):
    try:
        return Rule.objects.for_org(org).select_related("destination").get(id=rule_id)
    except Rule.DoesNotExist:
        raise Http404 from None


@staff_member_required
def group(request, rule_id):
    org = current_org(request)
    rule = _get_rule(org, rule_id)
    if request.method == "POST":
        valid = niches.valid_keys()
        rule.niches = [k for k in request.POST.getlist("niches") if k in valid]
        picked = [s for s in request.POST.getlist("stores") if s in STORES]
        rule.stores = ",".join(picked) or "mercadolivre"
        rule.daily_cap = _int(request.POST.get("daily_cap"), 1, Rule.DAILY_CEILING, rule.daily_cap)
        rule.window_start = _int(request.POST.get("window_start"), 0, 23, rule.window_start)
        rule.window_end = _int(request.POST.get("window_end"), 0, 23, rule.window_end)
        rule.min_discount_pct = _int(request.POST.get("min_discount"), 1, 90, 15)
        rule.interval_minutes = _int(request.POST.get("interval"), 1, 1440, rule.interval_minutes)
        rule.active = request.POST.get("active") == "on"
        mode = request.POST.get("coupon_mode")
        rule.coupon_mode = mode if mode in dict(Rule.COUPON_MODES) else rule.coupon_mode
        low, high = _money(request.POST.get("price_min")), _money(request.POST.get("price_max"))
        if low and high and low > high:
            low, high = high, low
        rule.price_min, rule.price_max = low, high
        rule.terms = _terms(request.POST.get("terms"))
        rule.negative_terms = _terms(request.POST.get("negative_terms"))
        days = {d for d in request.POST.getlist("weekdays") if d in WEEKDAYS}
        rule.weekdays = ",".join(sorted(days)) if len(days) < 7 else ""
        rule.save()
        messages.success(request, "Salvo.")
        return redirect("panel:group", rule_id=rule.id)

    selected = set(rule.niches or [])
    tree = [
        {
            "label": macro, "key": mkey, "checked": mkey in selected,
            "count": (mkey in selected) + sum(k in selected for _l, k in subs),
            "subs": [{"label": label, "key": key, "checked": key in selected}
                     for label, key in subs],
        }
        for macro, mkey, subs in niches.catalog()
    ]
    explain = [
        (REASONS.get(reason, reason), count)
        for reason, count in sorted(
            explain_rule(rule).items(), key=lambda kv: (kv[0] != "prontas", -kv[1])
        )
    ]
    sent = [
        {"at": item.sent_at.astimezone(BRT).strftime("%d/%m %H:%M"), "name": _offer_name(item.body)}
        for item in OutboxItem.objects.for_org(org)
        .filter(rule=rule, state=OutboxItem.CONFIRMED, sent_at__isnull=False)
        .order_by("-sent_at")[:10]
    ]
    return render(request, "panel/group.html", {
        "org": org, "rule": rule, "tree": tree, "explain": explain, "sent": sent,
        "stores": [(s, STORE_LABELS[s], s in rule.store_list) for s in STORES],
        "hours": range(24), "coupon_modes": Rule.COUPON_MODES,
        "weekdays": [(k, label, k in rule.weekdays.split(",") or not rule.weekdays)
                     for k, label in WEEKDAYS.items()],
    })


@staff_member_required
@require_POST
def send_now(request, rule_id):
    org = current_org(request)
    rule = _get_rule(org, rule_id)
    item = enqueue_for_rule(rule, force=True)
    if item is None:
        top = explain_rule(rule)
        reasons = [REASONS.get(r, r) for r, _n in top.most_common(1) if r != "prontas"]
        messages.error(request, "Nada publicável agora" + (f": {reasons[0]}." if reasons else "."))
    else:
        stats = deliver_due(WahaTransport())
        if stats["confirmado"]:
            messages.success(request, "Enviado.")
        else:
            messages.error(request, "Na fila; envio não confirmado.")
    return redirect("panel:group", rule_id=rule.id)


@staff_member_required
def links(request):
    org = current_org(request)
    now = timezone.now()
    if request.method == "POST":
        stats = manual.import_pasted(org, request.POST.get("pasted", ""), now)
        if stats["importados"]:
            messages.success(request, f"{stats['importados']} importados.")
        if stats["rejeitados"] or not stats["importados"]:
            messages.error(request, f"{stats['rejeitados']} linhas recusadas.")
        return redirect("panel:links")
    rows = manual.pending(org, now)
    return render(request, "panel/links.html", {
        "org": org, "rows": rows, "text": "\n".join(url for url, _key in rows),
    })
