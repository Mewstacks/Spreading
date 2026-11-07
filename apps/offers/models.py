from django.db import models

from apps.orgs.models import OrgScopedModel


class Rule(OrgScopedModel):
    """Regra de divulgação (espelha a ConfiguracaoEnvio do v1, enxuta)."""

    OFFERS, COUPON_NOTICE = "ofertas", "aviso_cupons"
    TYPES = [(OFFERS, "Ofertas"), (COUPON_NOTICE, "Aviso de cupons")]

    name = models.CharField(max_length=120)
    destination = models.ForeignKey("orgs.Destination", on_delete=models.CASCADE)
    type = models.CharField(max_length=20, choices=TYPES, default=OFFERS)
    # Termos separados por vírgula (OU). Vazio = qualquer produto.
    terms = models.TextField(blank=True, default="")
    negative_terms = models.TextField(blank=True, default="")
    price_min = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_max = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    min_discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=15)
    # Janela local [início, fim) em horas; fim <= início cruza a meia-noite.
    window_start = models.PositiveSmallIntegerField(default=8)
    window_end = models.PositiveSmallIntegerField(default=20)
    # Dias ISO (1=segunda … 7=domingo), vírgula. Vazio = todos.
    weekdays = models.CharField(max_length=20, blank=True, default="")
    interval_minutes = models.PositiveIntegerField(default=60)
    daily_cap = models.PositiveIntegerField(default=12)
    cooldown_hours = models.PositiveIntegerField(default=360)
    active = models.BooleanField(default=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    DAILY_CEILING = 30

    def __str__(self):
        return self.name

    @property
    def effective_cap(self) -> int:
        return min(self.daily_cap, self.DAILY_CEILING)


class AffiliateLink(OrgScopedModel):
    """Link do dono (meli.la gerado no Link Builder) para um produto/URL."""

    product_key = models.CharField(max_length=300)
    affiliate_url = models.URLField(max_length=500)
    etiqueta = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "product_key"], name="uniq_affiliate_link")
        ]


class Offer(models.Model):
    """Oferta do catálogo compartilhado (feed do dia ou catálogo da API)."""

    store = models.CharField(max_length=30, default="mercadolivre")
    source = models.CharField(max_length=40)
    product_key = models.CharField(max_length=300)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=600)
    price_from = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_to = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    vertical = models.CharField(max_length=80, blank=True)
    valid_on = models.DateField()
    coupon_code = models.CharField(max_length=60, blank=True)
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "product_key", "valid_on"], name="uniq_offer_per_day"
            )
        ]
