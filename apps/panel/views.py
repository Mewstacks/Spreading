"""Painel enxuto: escolher, por grupo, nichos, lojas e cadência."""
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
from apps.linkbuilder.models import BuilderState
from apps.offers import niches
from apps.offers.models import STORE_LABELS, STORES, Rule
from apps.orgs.models import Destination, Organization

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
    "preco_inconclusivo": "Preço inconclusivo",
    "preco_diverge_da_prova": "Preço mudou",
    "sem_desconto": "Sem desconto",
    "ja_enfileirada": "Já na fila",
}


BUILDER_LABELS = {
    "pronto": "Pronto", "ocioso": "Ocioso", "desligado": "Desligado", "sem_sessao": "Sem sessão",
    "login": "Entrar de novo", "verificacao": "Verificação", "indisponivel": "Fora do ar",
}
BUILDER_FINE = {"pronto", "ocioso"}


def _builder_status(org):
    state = BuilderState.objects.for_org(org).first()
    if state is None:
        return None
    return {
        "label": BUILDER_LABELS.get(state.state, state.state), "detail": state.detail,
        "tone": "on" if state.state in BUILDER_FINE else (
            "off" if state.state == BuilderState.OFF else "warn"),
    }


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
    return render(request, "panel/groups.html", {
        "org": org, "cards": cards, "builder": _builder_status(org),
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
    return render(request, "panel/group.html", {
        "org": org, "rule": rule, "tree": tree, "explain": explain,
        "stores": [(s, STORE_LABELS[s], s in rule.store_list) for s in STORES],
        "hours": range(24),
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
