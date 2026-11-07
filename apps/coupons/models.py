from django.db import models


class Coupon(models.Model):
    """Cupom do catálogo compartilhado (não pertence a uma organização)."""

    SITE, CONTAINER, PRODUCTS = "site", "container", "products"
    OFFICIAL, TWO_SOURCES, ONE_SOURCE = "oficial", "2_fontes", "1_fonte"

    store = models.CharField(max_length=30, default="mercadolivre")
    source = models.CharField(max_length=60)
    code = models.CharField(max_length=60)
    discount_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    min_purchase = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    max_discount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    starts_on = models.DateField(null=True, blank=True)
    valid_until = models.DateField()
    container_url = models.URLField(max_length=500, blank=True)
    scope = models.CharField(max_length=12, default=CONTAINER)
    confidence = models.CharField(max_length=10, default=OFFICIAL)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "code", "valid_until"], name="uniq_coupon_code_validity"
            )
        ]

    def __str__(self):
        return f"{self.code} ({self.store})"
