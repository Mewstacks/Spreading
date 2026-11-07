from django.db import models
from django.utils import timezone

from apps.orgs.models import OrgScopedModel


class LinkAttempt(OrgScopedModel):
    """Última tentativa de gerar o link do dono para uma oferta (produto ou produto+cupom)."""

    OK, REFUSED, FAILED = "ok", "recusado", "falhou"

    link_key = models.CharField(max_length=340)
    status = models.CharField(max_length=10)
    reason = models.CharField(max_length=200, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_try_at = models.DateTimeField()
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "link_key"], name="uniq_link_attempt")
        ]


class BuilderState(OrgScopedModel):
    """Saúde do Link Builder da organização: o painel mostra e o runner respeita a pausa."""

    READY, OFF, NO_SESSION, LOGIN, CHECK, DOWN = (
        "pronto", "desligado", "sem_sessao", "login", "verificacao", "indisponivel",
    )

    state = models.CharField(max_length=14, default=OFF)
    detail = models.CharField(max_length=200, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta(OrgScopedModel.Meta):
        constraints = [models.UniqueConstraint(fields=["org"], name="uniq_builder_state")]
