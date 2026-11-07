"""Gera, com a conta do próprio afiliado, o link `meli.la` das ofertas que só faltam o link do dono.

Regras de convivência com o Programa de Afiliados (termos 1.9 e 3.1c): só o que as regras ativas
vão publicar, no máximo `LINKBUILDER_MAX_PER_RUN` por execução e `LINKBUILDER_MAX_PER_DAY` por dia,
passo humano entre links, parada na primeira verificação do ML e chave geral
`LINKBUILDER_ENABLED`. O link só entra no estoque depois que o destino confirma ser a página do
produto (nunca a vitrine `/social/` do afiliado).
"""
import random
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.coupons.feed import BRT
from apps.offers import selection
from apps.offers.links import safe_affiliate_url
from apps.offers.models import AffiliateLink, Rule

from .bait import bait_url
from .errors import Blocked, LoginRequired, NoSession, Rejected, Unavailable
from .models import BuilderState, LinkAttempt
from .verify import verify_landing

ETIQUETA = "linkbuilder"
ETIQUETA_MANUAL = "manual"
REFUSED_WAIT = timedelta(days=14)
UNSAFE_WAIT = timedelta(days=30)
MAX_FAILS_IN_A_ROW = 3
MAX_UNCLEAR_CHECKS = 2
IDLE = "ocioso"  # ligado e sem nada a gerar: não prova que a sessão está boa


def _state(org, now) -> BuilderState:
    state, _ = BuilderState.unscoped.get_or_create(
        org=org, defaults={"state": BuilderState.OFF, "updated_at": now}
    )
    return state


def _set(state, value, detail="", pause=None, now=None):
    state.state, state.detail = value, detail[:200]
    state.paused_until = now + pause if pause else None
    state.updated_at = now
    state.save()


def _record(org, link_key, status, reason, now, wait=None):
    """Grava a tentativa. Sem `wait`, o recuo cresce 1 h a cada tentativa (teto de 24 h)."""
    attempt, _ = LinkAttempt.unscoped.get_or_create(
        org=org, link_key=link_key, defaults={"status": status, "next_try_at": now}
    )
    attempt.status, attempt.reason = status, reason[:200]
    attempt.attempts += 1
    if wait is None:
        wait = timedelta(hours=min(attempt.attempts, 24))
    attempt.next_try_at, attempt.updated_at = now + wait, now
    attempt.save()


def _daily_budget(org, now) -> int:
    start = now.astimezone(BRT).replace(hour=0, minute=0, second=0, microsecond=0)
    done = LinkAttempt.objects.for_org(org).filter(
        status=LinkAttempt.OK, updated_at__gte=start
    ).count()
    return settings.LINKBUILDER_MAX_PER_DAY - done


def wanted(org, now, limit: int, respect_waits: bool = True) -> list:
    """Ofertas do ML que as regras ativas publicariam se tivessem link, pares cupom primeiro."""
    waiting = LinkAttempt.objects.for_org(org).filter(next_try_at__gt=now)
    held = set(waiting.values_list("link_key", flat=True)) if respect_waits else set()
    rows = []
    rules = Rule.unscoped.filter(org=org, active=True, type=Rule.OFFERS).select_related(
        "destination", "org"
    )
    for rule in rules:
        if rule.destination.active:
            for _ready in selection.candidates(org, rule.destination, rule, now, missing=rows):
                pass
    rows.sort(key=lambda row: (not row[0].coupon_kind, -row[1]))
    out, seen = [], set()
    for offer, _score in rows:
        if offer.link_key in seen or offer.link_key in held:
            continue
        seen.add(offer.link_key)
        out.append(offer)
        if len(out) >= limit:
            break
    return out


def _default_driver():
    from .pw import PlaywrightDriver

    return PlaywrightDriver(settings.LINKBUILDER_STATE)


def run(org, now=None, driver_factory=None, http=None, sleep=time.sleep) -> dict:
    now = now or timezone.now()
    state = _state(org, now)
    report = {"estado": state.state, "tentadas": 0, "geradas": 0, "recusadas": 0, "falhas": 0}
    if not settings.LINKBUILDER_ENABLED:
        _set(state, BuilderState.OFF, "desligado (LINKBUILDER_ENABLED=0)", None, now)
        report["estado"] = BuilderState.OFF
        return report
    if state.paused_until and now < state.paused_until:
        report["pausado_ate"] = state.paused_until.isoformat()
        return report
    budget = _daily_budget(org, now)
    if budget <= 0:
        report["estado"] = "teto_diario"
        return report
    todo = wanted(org, now, min(settings.LINKBUILDER_MAX_PER_RUN, budget))
    if not todo:
        if state.state == BuilderState.OFF:
            _set(state, IDLE, "", None, now)
        report["estado"] = state.state
        return report

    driver = (driver_factory or _default_driver)()
    try:
        _work(org, state, driver, todo, now, report, http, sleep)
    finally:
        driver.close()
    report["estado"] = state.state
    return report


def _work(org, state, driver, todo, now, report, http, sleep):
    try:
        driver.open()
    except NoSession as exc:
        return _set(state, BuilderState.NO_SESSION, str(exc), timedelta(minutes=5), now)
    except LoginRequired as exc:
        return _set(state, BuilderState.LOGIN, str(exc), timedelta(minutes=30), now)
    except Blocked as exc:
        return _set(state, BuilderState.CHECK, str(exc), timedelta(hours=2), now)
    except Unavailable as exc:
        return _set(state, BuilderState.DOWN, str(exc), timedelta(minutes=30), now)

    fails = unclear = 0
    pace = settings.LINKBUILDER_PACE_SECONDS
    for offer in todo:
        report["tentadas"] += 1
        bait = bait_url(offer)
        if not bait:
            _record(org, offer.link_key, LinkAttempt.REFUSED, "url_nao_afiliavel", now, UNSAFE_WAIT)
            report["recusadas"] += 1
            continue
        try:
            raw = driver.generate(bait)
        except Rejected as exc:
            _record(org, offer.link_key, LinkAttempt.REFUSED, str(exc), now, REFUSED_WAIT)
            report["recusadas"] += 1
            fails = 0
            continue
        except LoginRequired as exc:
            return _set(state, BuilderState.LOGIN, str(exc), timedelta(minutes=30), now)
        except Blocked as exc:
            return _set(state, BuilderState.CHECK, str(exc), timedelta(hours=2), now)
        except Unavailable as exc:
            _record(org, offer.link_key, LinkAttempt.FAILED, str(exc), now)
            report["falhas"] += 1
            fails += 1
            if fails >= MAX_FAILS_IN_A_ROW:
                return _set(state, BuilderState.DOWN, str(exc), timedelta(hours=1), now)
            continue
        fails = 0
        link = safe_affiliate_url(raw)
        if not link:
            _record(org, offer.link_key, LinkAttempt.FAILED, "resultado_invalido", now)
            report["falhas"] += 1
            continue
        if settings.LINKBUILDER_VERIFY:
            verdict, detail = verify_landing(link, http=http)
            if verdict == "recusado":
                _record(org, offer.link_key, LinkAttempt.REFUSED, detail, now, REFUSED_WAIT)
                report["recusadas"] += 1
                unclear = 0
                continue
            if verdict == "inconclusivo":
                _record(org, offer.link_key, LinkAttempt.FAILED, f"verificacao: {detail}", now)
                report["falhas"] += 1
                unclear += 1
                if unclear >= MAX_UNCLEAR_CHECKS:
                    return _set(state, BuilderState.CHECK, detail, timedelta(hours=2), now)
                continue
            unclear = 0
        AffiliateLink.unscoped.update_or_create(
            org=org, product_key=offer.link_key,
            defaults={"affiliate_url": link, "etiqueta": ETIQUETA},
        )
        _record(org, offer.link_key, LinkAttempt.OK, "", now, timedelta(0))
        report["geradas"] += 1
        if pace:
            sleep(random.uniform(0.7 * pace, 1.5 * pace))
    _set(state, BuilderState.READY, "", None, now)
