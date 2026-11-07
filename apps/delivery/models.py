from django.db import models
from django.utils import timezone

from apps.orgs.models import OrgScopedModel


class OutboxItem(OrgScopedModel):
    QUEUED, SENDING, CONFIRMED, UNCERTAIN, FAILED = (
        "queued", "sending", "confirmado", "incerto", "falhou"
    )
    STATES = [(s, s) for s in (QUEUED, SENDING, CONFIRMED, UNCERTAIN, FAILED)]

    destination = models.ForeignKey("orgs.Destination", on_delete=models.PROTECT)
    rule = models.ForeignKey("offers.Rule", null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=20, default="coupon_notice")
    body = models.TextField()
    image_url = models.URLField(max_length=600, blank=True, default="")
    idempotency_key = models.CharField(max_length=64, unique=True)
    state = models.CharField(max_length=12, choices=STATES, default=QUEUED, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    external_message_id = models.CharField(max_length=120, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScopedModel.Meta):
        indexes = [models.Index(fields=["state", "next_attempt_at"])]
        constraints = [
            # no máximo 1 item pendente/enviando por destino
            models.UniqueConstraint(
                fields=["destination"],
                condition=models.Q(state__in=["queued", "sending"]),
                name="one_pending_per_destination",
            )
        ]


class CouponDelivery(OrgScopedModel):
    """Um código já enfileirado/entregue a um destino não volta."""

    destination = models.ForeignKey("orgs.Destination", on_delete=models.PROTECT)
    coupon = models.ForeignKey("coupons.Coupon", on_delete=models.PROTECT)
    outbox_item = models.ForeignKey(OutboxItem, on_delete=models.CASCADE)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["destination", "coupon"], name="uniq_coupon_delivery")
        ]


class OfferDelivery(OrgScopedModel):
    """Cooldown do mesmo produto por destino."""

    destination = models.ForeignKey("orgs.Destination", on_delete=models.PROTECT)
    product_key = models.CharField(max_length=300)
    outbox_item = models.ForeignKey(OutboxItem, on_delete=models.CASCADE)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta(OrgScopedModel.Meta):
        indexes = [models.Index(fields=["destination", "product_key", "created_at"])]


class Alert(models.Model):
    """Problema em curso que o dono já foi (ou vai ser) avisado: some quando se resolve."""

    key = models.CharField(max_length=80, unique=True)  # "wa:default", "fonte:amazon", "incerto"
    since = models.DateTimeField()
    last_sent = models.DateTimeField(null=True, blank=True)
