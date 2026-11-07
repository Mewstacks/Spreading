"""Saúde de verdade: sessão viva, última entrega confirmada, fila e frescor das fontes."""
import time
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.utils import timezone

from apps.coupons.models import Coupon
from apps.offers.models import Offer

from .models import OutboxItem


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
    status = transport.session_status() if hasattr(transport, "session_status") else ""
    data = {
        "db": "ok",
        "whatsapp_session": status or "inalcancavel",
        "last_confirmed_age_s": int((now - last_ok.sent_at).total_seconds()) if last_ok else None,
        "queued": OutboxItem.unscoped.filter(state=OutboxItem.QUEUED).count(),
        "uncertain": OutboxItem.unscoped.filter(state=OutboxItem.UNCERTAIN).count(),
        "failed_24h": OutboxItem.unscoped.filter(
            state=OutboxItem.FAILED, created_at__gte=now - timedelta(hours=24)
        ).count(),
        "coupon_feed_fresh": bool(coupon_seen and now - coupon_seen <= stale_after),
        "offer_feed_fresh": bool(offer_seen and now - offer_seen <= stale_after),
    }
    data["ok"] = data["whatsapp_session"] == "WORKING" and data["coupon_feed_fresh"]
    return data


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
