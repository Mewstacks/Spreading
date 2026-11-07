"""Fontes: de onde vêm as ofertas e os cupons, e se cada uma está viva."""
from datetime import timedelta

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone

from apps.coupons.feed import BRT
from apps.offers import ml_api, shopee
from apps.offers.models import SourceRun

STALE = timedelta(hours=6)  # as fontes rodam de hora em hora: 6 h sem resultado é silêncio demais
SOURCES = (
    ("cupons", "ML · cupons", "ticket", lambda: True),
    ("ofertas_dia", "ML · ofertas do dia", "flame", lambda: True),
    ("catalogo", "ML · catálogo", "boxes", ml_api.configured),
    ("ml_vitrine", "ML · vitrine", "store", lambda: bool(settings.ML_PUBLIC_PAGES)),
    ("amazon", "Amazon · busca", "search", lambda: bool(settings.AMAZON_COLLECT)),
    ("amazon_cupons", "Amazon · cupons", "ticket-percent", lambda: bool(settings.AMAZON_COLLECT)),
    ("shopee", "Shopee · API", "shopping-bag", shopee.configured),
)


def _when(moment) -> str:
    return moment.astimezone(BRT).strftime("%d/%m %H:%M") if moment else ""


def source_state(run, enabled, now):
    """(texto, tom) do estado de uma fonte."""
    if not enabled:
        return "Desligada", "muted"
    if run is None:
        return "Sem execução", "muted"
    if not run.ok:
        return f"Falhando {run.failures}×", "red"
    if run.result_at is None or now - run.result_at > STALE:
        return "Sem novidade", "yellow"
    return "Ok", "green"


@staff_member_required
def sources(request):
    now = timezone.now()
    runs = {run.label: run for run in SourceRun.objects.all()}
    rows = []
    for label, name, icon, is_enabled in SOURCES:
        run = runs.get(label)
        state, tone = source_state(run, is_enabled(), now)
        rows.append({
            "name": name, "icon": icon, "state": state, "tone": tone,
            "result": run.result if run else "", "at": _when(run.result_at) if run else "",
            "error": run.error if run and not run.ok else "",
            "checked": _when(run.checked_at) if run else "",
        })
    return render(request, "panel/sources.html", {"rows": rows})
