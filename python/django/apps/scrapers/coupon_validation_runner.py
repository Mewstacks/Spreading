"""Executor transacional da fila de validação, sem concluir compras.

Os adaptadores de loja apenas observam o carrinho e devolvem subtotais. Este
módulo controla concorrência, recuperação de worker e o limite de segurança; o
veredito monetário continua centralizado em ``coupon_validation``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping

from django.db import transaction
from django.db.models import Q
from django.db.models import F
from django.utils import timezone

from .coupon_validation import registrar_resultado
from .models import CupomValidacao


logger = logging.getLogger(__name__)
STALE_RUNNING_MINUTES = 15


@dataclass(frozen=True)
class ValidationObservation:
    status: str
    reason_code: str = ""
    safe_detail: str = ""
    subtotal_before: Decimal | str | float | None = None
    subtotal_after: Decimal | str | float | None = None
    evidence: dict = field(default_factory=dict)


Validator = Callable[[CupomValidacao], ValidationObservation]


def _checkout_session_available(user, marketplace):
    if marketplace == "mercadolivre":
        from apps.accounts.ml_sessions import has_storage_state
        return has_storage_state(user)
    if marketplace == "amazon":
        from apps.scrapers.report_sessions import has_report_session
        return has_report_session(user, "amazon_shop")
    return True


def defer_missing_checkout_sessions(*, now=None):
    """Retira em lote da cabeça da fila quem exige login humano ausente."""
    from django.contrib.auth import get_user_model

    now = now or timezone.now()
    retry_at = now + timezone.timedelta(hours=6)
    pairs = list(CupomValidacao.objects.filter(
        status="pending", marketplace__in=("mercadolivre", "amazon"),
    ).values_list("usuario_id", "marketplace").distinct())
    users = {
        user.id: user for user in get_user_model().objects.filter(
            id__in={user_id for user_id, _ in pairs}, is_active=True,
        )
    }
    deferred = {}
    for user_id, marketplace in pairs:
        user = users.get(user_id)
        if user is None or _checkout_session_available(user, marketplace):
            continue
        count = CupomValidacao.objects.filter(
            usuario_id=user_id, marketplace=marketplace, status="pending",
        ).update(
            status="inconclusive", reason_code="session_required",
            safe_detail="Conecte a conta da loja para validar no carrinho.",
            verified_at=now, retry_at=retry_at, started_at=None,
            attempts=F("attempts") + 1, no_purchase=True,
        )
        deferred[marketplace] = deferred.get(marketplace, 0) + count
    return deferred


def claim_pending_validations(*, marketplaces, limit=3, now=None):
    """Reserva um lote sem deixar dois workers testarem o mesmo carrinho."""
    now = now or timezone.now()
    marketplaces = {
        str(value or "").strip().casefold() for value in marketplaces if value
    }
    if not marketplaces or limit <= 0:
        return []
    stale_before = now - timezone.timedelta(minutes=STALE_RUNNING_MINUTES)
    with transaction.atomic():
        CupomValidacao.objects.filter(
            status="running", started_at__lt=stale_before,
        ).update(
            status="pending", reason_code="worker_recovered",
            safe_detail="Tentativa anterior interrompida; devolvida à fila.",
            started_at=None, retry_at=None,
        )
        rows = list(CupomValidacao.objects.select_for_update(skip_locked=True).filter(
            Q(retry_at__isnull=True) | Q(retry_at__lte=now),
            status="pending", marketplace__in=marketplaces,
        ).select_related("cupom", "usuario").order_by(
            "created_at", "pk",
        )[:limit])
        for row in rows:
            row.status = "running"
            row.reason_code = ""
            row.safe_detail = ""
            row.started_at = now
        if rows:
            CupomValidacao.objects.bulk_update(
                rows, ("status", "reason_code", "safe_detail", "started_at"),
                batch_size=limit,
            )
    return rows


def _failure_observation(reason_code, safe_detail):
    return ValidationObservation(
        status="inconclusive", reason_code=reason_code,
        safe_detail=safe_detail,
        evidence={"no_purchase_boundary": True},
    )


def run_validation_batch(*, adapters: Mapping[str, Validator], limit=3):
    """Executa um lote pequeno; falha de uma loja não prende as demais."""
    normalized = {
        str(key or "").strip().casefold(): value
        for key, value in dict(adapters or {}).items() if callable(value)
    }
    rows = claim_pending_validations(
        marketplaces=normalized.keys(), limit=limit,
    )
    metrics = {
        "claimed": len(rows), "accepted": 0, "rejected": 0,
        "inconclusive": 0, "adapter_errors": 0,
    }
    for row in rows:
        adapter = normalized[row.marketplace]
        try:
            observation = adapter(row)
            if not isinstance(observation, ValidationObservation):
                raise TypeError("validator must return ValidationObservation")
            if observation.status not in {"accepted", "rejected", "inconclusive"}:
                raise ValueError("validator returned an invalid status")
            if not isinstance(observation.evidence, dict):
                raise TypeError("validator evidence must be a dictionary")
        except Exception as exc:
            # Nunca persistir a mensagem da exceção: navegador/HTTP pode incluir
            # URL, cookies ou dados da conta. O tipo basta para o log operacional.
            logger.error(
                "Validação de checkout falhou marketplace=%s validation=%s type=%s",
                row.marketplace, row.pk, type(exc).__name__,
            )
            metrics["adapter_errors"] += 1
            observation = _failure_observation(
                "adapter_error",
                "Falha temporária ao observar o carrinho; nova tentativa agendada.",
            )
        result = registrar_resultado(
            row, status=observation.status,
            reason_code=observation.reason_code,
            safe_detail=observation.safe_detail,
            subtotal_before=observation.subtotal_before,
            subtotal_after=observation.subtotal_after,
            evidence=observation.evidence,
        )
        metrics[result.status] += 1
    return metrics
