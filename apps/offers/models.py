from django.db import models

from apps.orgs.models import OrgScopedModel

STORES = ("mercadolivre", "amazon", "shopee")
STORE_LABELS = {"mercadolivre": "Mercado Livre", "amazon": "Amazon", "shopee": "Shopee"}


class Rule(OrgScopedModel):
    """Configuração de um grupo: nichos, lojas e cadência (espelha a ConfiguracaoEnvio do v1)."""

    OFFERS, COUPON_NOTICE = "ofertas", "aviso_cupons"
    TYPES = [(OFFERS, "Ofertas"), (COUPON_NOTICE, "Aviso de cupons")]

    name = models.CharField(max_length=120)
    destination = models.ForeignKey("orgs.Destination", on_delete=models.CASCADE)
    type = models.CharField(max_length=20, choices=TYPES, default=OFFERS)
    # Nichos escolhidos no painel: chaves "m:<Macro>" / "s:<Macro>|<Rótulo>". Vazio = tudo.
    niches = models.JSONField(default=list, blank=True)
    # Lojas habilitadas, separadas por vírgula.
    stores = models.CharField(max_length=80, default="mercadolivre,amazon,shopee")
    # Avançado: termos extras e negativos (vírgula).
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

    # Cupom: "first" = par cupom+produto antes das demais; "only" = só cupom; "off" = sem cupom.
    COUPON_MODES = [("first", "Primeiro"), ("only", "Só cupom"), ("off", "Sem cupom")]
    coupon_mode = models.CharField(max_length=5, choices=COUPON_MODES, default="first")

    DAILY_CEILING = 30

    def __str__(self):
        return self.name

    @property
    def effective_cap(self) -> int:
        return min(self.daily_cap, self.DAILY_CEILING)

    @property
    def store_list(self) -> list[str]:
        return [s for s in self.stores.split(",") if s in STORES]


class AffiliateLink(OrgScopedModel):
    """Link do dono (meli.la) para um produto, ou produto+cupom (`link_key` com sufixo)."""

    product_key = models.CharField(max_length=340)
    affiliate_url = models.URLField(max_length=500)
    etiqueta = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "product_key"], name="uniq_affiliate_link")
        ]


class Offer(models.Model):
    """Oferta do catálogo compartilhado (feed do dia, catálogo da API ou par cupom+produto)."""

    store = models.CharField(max_length=30, default="mercadolivre")
    source = models.CharField(max_length=40)
    product_key = models.CharField(max_length=300)
    # Chave do link do dono: o produto, ou produto+cupom (o link carrega a campanha do cupom).
    link_key = models.CharField(max_length=340)
    # URL que entra no Link Builder para gerar o link (produto ou produto com campanha).
    source_url = models.URLField(max_length=1500, blank=True)
    title = models.CharField(max_length=300)
    url = models.URLField(max_length=600)
    image_url = models.URLField(max_length=600, blank=True, default="")
    macro = models.CharField(max_length=80, blank=True)
    vertical = models.CharField(max_length=80, blank=True)
    price_from = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    price_to = models.DecimalField(max_digits=12, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    # Comissão do afiliado em % quando a loja informa (Shopee); desempata o ranking.
    commission_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # Cupom pareado ao produto (comprovado): código ou ativação no link.
    coupon_kind = models.CharField(max_length=12, blank=True)  # "" | "code" | "activation"
    coupon_code = models.CharField(max_length=60, blank=True)
    coupon_pct = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    coupon_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    coupon_cap = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    coupon_min = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    coupon_until = models.DateTimeField(null=True, blank=True)
    # Preço do anúncio no momento da prova do cupom (preço ao vivo tem de bater).
    proof_base_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    proof_at = models.DateTimeField(null=True, blank=True)
    valid_on = models.DateField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "link_key", "valid_on"], name="uniq_offer_per_day"
            )
        ]
        indexes = [models.Index(fields=["store", "last_seen"])]


class Conversion(OrgScopedModel):
    """Venda atribuída ao dono, vinda do relatório da loja (Shopee `conversionReport`)."""

    store = models.CharField(max_length=30, default="shopee")
    external_id = models.CharField(max_length=80)  # conversionId:orderId
    purchased_at = models.DateTimeField()
    status = models.CharField(max_length=20)  # PENDING | COMPLETED | CANCELLED (texto da loja)
    title = models.CharField(max_length=300, blank=True)  # primeiro item do pedido
    items = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)  # valor vendido
    commission = models.DecimalField(max_digits=12, decimal_places=4, default=0)  # líquida
    channel = models.CharField(max_length=40, blank=True)  # origem do clique (WhatsApp, YouTube…)
    sub_id = models.CharField(max_length=80, blank=True)  # utm_content do link
    synced_at = models.DateTimeField()

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "store", "external_id"], name="uniq_conversion")
        ]
        indexes = [models.Index(fields=["org", "store", "purchased_at"])]


class TermRun(models.Model):
    """Última coleta de catálogo por termo: a API do ML não é consultada a cada ciclo."""

    term = models.CharField(max_length=120, unique=True)
    last_run = models.DateTimeField()


class SourceRun(models.Model):
    """Estado de cada fonte do ciclo: a tela Fontes lê isto, e os alertas também."""

    label = models.CharField(max_length=40, unique=True)
    checked_at = models.DateTimeField()  # última vez que o ciclo chamou a fonte
    ok = models.BooleanField(default=True)
    failures = models.PositiveIntegerField(default=0)  # falhas seguidas
    error = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=200, blank=True)  # último resultado não vazio
    result_at = models.DateTimeField(null=True, blank=True)
