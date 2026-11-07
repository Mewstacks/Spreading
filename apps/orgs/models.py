from django.db import models


class TenantScopeError(RuntimeError):
    """Consulta de modelo por organização sem informar a organização."""


class OrgScopedQuerySet(models.QuerySet):
    pass


class OrgScopedManager(models.Manager.from_queryset(OrgScopedQuerySet)):
    """Default-deny: `Model.objects.<qualquer coisa>` levanta erro.

    Use `Model.objects.for_org(org)` no código de aplicação e `Model.unscoped` só em jobs de
    sistema (ingestão, migração, admin).
    """

    def get_queryset(self):
        raise TenantScopeError(
            f"{self.model.__name__}: informe a organização com .for_org(org) "
            "(ou use .unscoped em job de sistema)."
        )

    def for_org(self, org):
        return OrgScopedQuerySet(self.model, using=self._db).filter(org=org)


class OrgScopedModel(models.Model):
    org = models.ForeignKey("orgs.Organization", on_delete=models.CASCADE)

    objects = OrgScopedManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True
        default_manager_name = "unscoped"


class Organization(models.Model):
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    # Tag de afiliado da Amazon (link = /dp/ASIN?tag=<tag>).
    amazon_tag = models.CharField(max_length=60, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class Destination(OrgScopedModel):
    TELEGRAM = "telegram"
    WA_GROUP = "whatsapp_group"
    WA_CHANNEL = "whatsapp_channel"
    KINDS = [(TELEGRAM, "Telegram"), (WA_GROUP, "Grupo WhatsApp"), (WA_CHANNEL, "Canal WhatsApp")]

    kind = models.CharField(max_length=20, choices=KINDS)
    external_id = models.CharField(max_length=200)
    name = models.CharField(max_length=120)
    active = models.BooleanField(default=True)
    # Sessão do WAHA (um número de WhatsApp) que envia para este destino.
    session = models.CharField(max_length=40, default="default")
    # Política de cadência por destino.
    daily_cap = models.PositiveIntegerField(default=12)
    min_interval_seconds = models.PositiveIntegerField(default=60)
    next_allowed_at = models.DateTimeField(null=True, blank=True)
    paused_until = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "kind", "external_id"], name="uniq_destination")
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"


class ContainerLink(OrgScopedModel):
    """Link de afiliado do dono para um container de cupom (colado uma vez no Link Builder)."""

    container_url = models.URLField(max_length=500)
    affiliate_url = models.URLField(max_length=500)
    etiqueta = models.CharField(max_length=80, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta(OrgScopedModel.Meta):
        constraints = [
            models.UniqueConstraint(fields=["org", "container_url"], name="uniq_container_link")
        ]
