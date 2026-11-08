"""Saúde de verdade: sessão viva, última entrega confirmada, fila e frescor das fontes."""
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.coupons.models import Coupon
from apps.offers.models import Offer, SourceRun

from .models import OutboxItem

CYCLE_STALE_S = 900  # o ciclo roda de 5 em 5 minutos: 15 minutos parado é problema


def snapshot(transport, now=None) -> dict:
    now = now or timezone.now()
    with connection.cursor() as cursor:
        cursor.execute("select 1")
    last_ok = (
        OutboxItem.unscoped.filter(state=OutboxItem.CONFIRMED).order_by("-sent_at").first()
    )
    coupon_seen = Coupon.objects.order_by("-last_seen").values_list("last_seen", flat=True).first()
    offer_seen = Offer.objects.order_by("-last_seen").values_list("last_seen", flat=True).first()
    stale_after = timedelta(seconds=settings.FEED_MAX_AGE_SECONDS * 2)
    sessions = {}
    if hasattr(transport, "used_sessions"):  # vários números: todos os que têm regra ativa
        sessions = {name: transport.for_session(name).session_status() or "inalcancavel"
                    for name in sorted(transport.used_sessions())}
        down = [status for status in sessions.values() if status != "WORKING"]
        status = (down[0] if down else "WORKING") if sessions else ""
    else:
        status = transport.session_status() if hasattr(transport, "session_status") else ""
    data = {
        "db": "ok",
        "whatsapp_session": status or "inalcancavel",
        "whatsapp_sessions": sessions,
        "last_confirmed_age_s": int((now - last_ok.sent_at).total_seconds()) if last_ok else None,
        "queued": OutboxItem.unscoped.filter(state=OutboxItem.QUEUED).count(),
        "uncertain": OutboxItem.unscoped.filter(state=OutboxItem.UNCERTAIN).count(),
        "failed_24h": OutboxItem.unscoped.filter(
            state=OutboxItem.FAILED, created_at__gte=now - timedelta(hours=24)
        ).count(),
        "coupon_feed_fresh": bool(coupon_seen and now - coupon_seen <= stale_after),
        "offer_feed_fresh": bool(offer_seen and now - offer_seen <= stale_after),
    }
    latest = SourceRun.objects.order_by("-checked_at").values_list("checked_at", flat=True).first()
    data["cycle_age_s"] = int((now - latest).total_seconds()) if latest else None  # o worker vive
    data["whatsapp_checks"] = {
        run.label[3:]: {"ok": run.ok, "failures": run.failures, "at": run.checked_at.isoformat()}
        for run in SourceRun.objects.filter(label__startswith="wa:")
    }
    cycle_ok = data["cycle_age_s"] is None or data["cycle_age_s"] <= CYCLE_STALE_S
    data["ok"] = data["whatsapp_session"] == "WORKING" and data["coupon_feed_fresh"] and cycle_ok
    return data


def canary_chats() -> dict[str, str]:
    """Chat privado de canário de cada número: `CANARY_CHATS=numero=chat,…`; `CANARY_CHAT_ID`
    vale para o número principal. Número sem chat é só sondado (nunca recebe mensagem)."""
    chats = {}
    for part in (getattr(settings, "CANARY_CHATS", "") or "").split(","):
        name, _sep, chat = part.partition("=")
        if name.strip() and chat.strip():
            chats[name.strip()] = chat.strip()
    if getattr(settings, "CANARY_CHAT_ID", ""):
        chats.setdefault(settings.WAHA_SESSION, settings.CANARY_CHAT_ID)
    return chats


def run_probe(transport) -> dict:
    """Sem chat de canário: sessão em WORKING e o WhatsApp respondendo pelo próprio número."""
    if not transport.session_working():
        return {"ok": False, "reason": "sessão fora de WORKING"}
    reachable = getattr(transport, "reachable", None)
    if reachable is not None and not reachable():
        return {"ok": False, "reason": "o WhatsApp não responde"}
    return {"ok": True, "probe": True}


def run_canary(transport, chat_id: str, wait_s=20, poll_s=2, sleep=time.sleep) -> dict:
    """Mensagem real a um grupo privado e checagem de ACK: 'conectado' não prova entrega."""
    if not chat_id:
        return {"ok": False, "reason": "CANARY_CHAT_ID não configurado"}
    result = transport.send_text(chat_id, "✔ canário")
    if not result.success:
        return {"ok": False, "reason": f"envio falhou: {result.error}"}
    waited = 0
    while waited <= wait_s:
        ack = transport.message_ack(chat_id, result.message_id)
        if ack is not None and ack >= 2:
            return {"ok": True, "ack": ack}
        sleep(poll_s)
        waited += poll_s
    return {"ok": False, "reason": "sem ACK ≥ 2 no prazo", "message_id": result.message_id}
