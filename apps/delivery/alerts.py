"""Alertas opt-in: avisa o dono quando algo para (número fora do ar, fonte falhando, envio incerto).

Sem `ALERT_CHAT_ID` nem `ALERT_EMAIL_TO` nada é enviado. Cada problema avisa uma vez, repete a cada
6 h enquanto durar e manda um "voltou" quando passa. WhatsApp sai por um número conectado; se todos
os números caírem, só o e-mail chega, por isso vale configurar os dois.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail

from apps.offers.models import SourceRun

from .models import Alert, OutboxItem

logger = logging.getLogger(__name__)

REPEAT = timedelta(hours=6)
GRACE = {"wa": timedelta(minutes=10)}  # número reconectando não é incidente; o resto avisa na hora
STUCK_AFTER = timedelta(minutes=30)
FAILURES = 3


def configured() -> bool:
    return bool(getattr(settings, "ALERT_CHAT_ID", "") or getattr(settings, "ALERT_EMAIL_TO", ""))


def conditions(now, transport) -> dict[str, str]:
    """Problemas de agora: chave → texto curto."""
    found, sessions = {}, {}
    if hasattr(transport, "used_sessions"):
        for name in sorted(transport.used_sessions()):
            sender = transport.for_session(name)
            status = sessions[name] = sender.session_status()
            if status != "WORKING":
                found[f"wa:{name}"] = f"Número {name} fora do ar ({status or 'sem resposta'})."
            elif not getattr(sender, "reachable", lambda: True)():
                found[f"wa:{name}"] = f"Número {name} conectado, mas o WhatsApp não responde."
    # `wa:<número>` já vira "fora do ar" ou "conectado, mas não responde": não avisa duas vezes.
    failing = SourceRun.objects.filter(failures__gte=FAILURES).exclude(label__startswith="wa:")
    for run in failing:
        found[f"fonte:{run.label}"] = f"Fonte {run.label} falhando {run.failures}×: {run.error}"
    uncertain = OutboxItem.unscoped.filter(state=OutboxItem.UNCERTAIN).count()
    if uncertain:
        found["incerto"] = f"{uncertain} envio(s) incerto(s): confira o grupo antes de reenviar."
    stuck = OutboxItem.unscoped.filter(
        state=OutboxItem.QUEUED, next_attempt_at__lte=now - STUCK_AFTER,
        destination__session__in=[n for n, status in sessions.items() if status == "WORKING"],
    ).count()
    if stuck:
        found["fila"] = f"{stuck} mensagem(ns) parada(s) na fila há mais de 30 min."
    return found


def _deliver(text, transport, down) -> bool:
    delivered = False
    chat = getattr(settings, "ALERT_CHAT_ID", "")
    if chat:
        sender = None
        if hasattr(transport, "used_sessions"):
            live = [n for n in sorted(transport.used_sessions()) if f"wa:{n}" not in down]
            sender = transport.for_session(live[0]) if live else None
        elif hasattr(transport, "send_text"):
            sender = transport
        if sender is not None:
            delivered = bool(sender.send_text(chat, f"⚠️ Spreading: {text}").success)
    target = getattr(settings, "ALERT_EMAIL_TO", "")
    if target:
        try:
            send_mail("Spreading: alerta", text, None,
                      [a.strip() for a in target.split(",") if a.strip()])
            delivered = True
        except Exception as exc:  # noqa: BLE001  e-mail ruim não derruba o ciclo
            logger.warning("alerta por e-mail falhou: %s", exc)
    return delivered


def run(transport, now) -> dict:
    """Avalia, avisa o que venceu a carência e limpa o que se resolveu. Nunca levanta."""
    if not configured():
        return {}
    try:
        found = conditions(now, transport)
        down = {key for key in found if key.startswith("wa:")}
        sent = 0
        for alert in Alert.objects.exclude(key__in=found):
            if alert.last_sent and _deliver(f"voltou ao normal ({alert.key}).", transport, down):
                sent += 1
            alert.delete()
        for key, text in found.items():
            alert, _created = Alert.objects.get_or_create(key=key, defaults={"since": now})
            waited = now - alert.since >= GRACE.get(key.split(":")[0], timedelta(0))
            if waited and (alert.last_sent is None or now - alert.last_sent >= REPEAT):
                if _deliver(text, transport, down):
                    alert.last_sent = now
                    alert.save(update_fields=["last_sent"])
                    sent += 1
        return {"ativos": len(found), "enviados": sent}
    except Exception as exc:  # noqa: BLE001
        logger.warning("alertas falharam: %s", exc)
        return {}
