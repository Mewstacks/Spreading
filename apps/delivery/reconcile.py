"""Reconciliação de envios `incerto`: o WAHA guarda as mensagens do chat, então achar a nossa lá
prova que entregou, e ela não estar lá, com a lista cobrindo o horário do envio, prova que não.

Nunca reenvia: só decide o estado. Item que chegou vira `confirmado` (o cooldown fica); item que não
chegou vira `falhou` e solta o produto/cupom para a seleção escolher de novo. Na dúvida (WAHA fora,
número fora do ar, lista que não alcança o envio, item novo demais) continua `incerto`.
"""
import logging
from datetime import datetime, timedelta, timezone

from django.db import transaction

from .models import CouponDelivery, OfferDelivery, OutboxItem

logger = logging.getLogger(__name__)

WINDOW = timedelta(hours=48)  # item incerto mais velho que isso não é mais olhado
MIN_AGE = timedelta(minutes=2)  # o WhatsApp precisa de um tempo para a mensagem aparecer no chat
GIVE_UP_AFTER = timedelta(minutes=10)  # só declara "não chegou" depois disto
LOOKBACK_S = 120  # a mensagem pode ter saído um pouco antes de o envio dar timeout
FETCH = 100  # mensagens pedidas por chat
PER_CYCLE = 5  # itens olhados por ciclo


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def _arrived(item, messages):
    """A mensagem nossa no chat: de nós, depois do enfileiramento e com a mesma legenda."""
    want = _norm(item.body)
    since = int(item.created_at.timestamp()) - LOOKBACK_S
    for message in sorted(messages, key=lambda m: m["ts"]):
        if message["from_me"] and message["ts"] >= since and _norm(message["body"]) == want:
            return message
    return None


def _covers(messages, item) -> bool:
    """A lista alcança o horário do envio? Lista curta é o histórico inteiro; lista cheia só vale
    se a mais antiga é de antes do envio (senão o chat anda rápido demais para concluir)."""
    if len(messages) < FETCH:
        return True
    return min(m["ts"] for m in messages) < int(item.created_at.timestamp()) - LOOKBACK_S


def _confirm(item, message):
    item.state = OutboxItem.CONFIRMED
    item.external_message_id = str(message["id"])[:120]
    item.sent_at = datetime.fromtimestamp(message["ts"], tz=timezone.utc)
    item.last_error = "reconciliado"
    item.save(update_fields=["state", "external_message_id", "sent_at", "last_error"])


def _fail(item):
    with transaction.atomic():
        item.state = OutboxItem.FAILED
        item.last_error = "nao_chegou (reconciliado)"
        item.save(update_fields=["state", "last_error"])
        CouponDelivery.unscoped.filter(outbox_item=item).delete()
        OfferDelivery.unscoped.filter(outbox_item=item).delete()


def _one(transport, item, now) -> str:
    if now - item.created_at < MIN_AGE:
        return "pendente"
    sender = (transport.for_session(item.destination.session)
              if hasattr(transport, "for_session") else transport)
    history = getattr(sender, "recent_messages", None)
    if history is None or not sender.session_working():
        return "pendente"
    messages = history(item.destination.external_id, FETCH)
    if messages is None:
        return "pendente"
    found = _arrived(item, messages)
    if found is not None:
        _confirm(item, found)
        return "confirmado"
    if now - item.created_at >= GIVE_UP_AFTER and _covers(messages, item):
        _fail(item)
        return "falhou"
    return "pendente"


def run(transport, now) -> dict:
    """Olha os itens incertos recentes (no máximo PER_CYCLE) e devolve a contagem por veredito."""
    stats = {"confirmado": 0, "falhou": 0, "pendente": 0}
    items = (OutboxItem.unscoped.select_related("destination")
             .filter(state=OutboxItem.UNCERTAIN, created_at__gte=now - WINDOW).order_by("id")
             [:PER_CYCLE])
    for item in items:
        stats[_one(transport, item, now)] += 1
    return stats
