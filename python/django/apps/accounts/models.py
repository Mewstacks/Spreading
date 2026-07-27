"""Perfil do usuário — extensão 1:1 do User para o SaaS multi-tenant.

Guarda: estado de verificação de e-mail, tags de afiliado por usuário (ML + Amazon),
sessão de WhatsApp do usuário e o último estado conhecido das conexões (p/ alertas).
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone
from django.utils.text import slugify
import uuid

from apps.accounts.fields import EncryptedCharField


class Organization(models.Model):
    """Fronteira de dados e autorização do SaaS."""

    STATUS = [
        ("active", "Ativa"),
        ("suspended", "Suspensa"),
        ("migration_blocked", "Migração bloqueada"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True)
    status = models.CharField(max_length=24, choices=STATUS, default="active",
                              db_index=True)
    personal_owner = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="personal_organization",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Membership(models.Model):
    """Permissão explícita de uma pessoa dentro de uma organização."""

    ROLES = [
        ("owner", "Owner"),
        ("operator", "Operador"),
        ("viewer", "Leitura"),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(max_length=16, choices=ROLES, default="viewer")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"], name="uniq_membership_org_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["organization", "is_active"]),
        ]

    def __str__(self):
        return f"{self.user_id}@{self.organization_id}:{self.role}"


class MercadoLivreSession(models.Model):
    """Storage state Playwright cifrado e autenticado por organização."""

    STATUS = [
        ("active", "Ativa"),
        ("expired", "Expirada"),
        ("decrypt_error", "Erro de criptografia"),
        ("rotation_required", "Rotação necessária"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="mercadolivre_session",
    )
    connection_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    cipher_version = models.CharField(max_length=24, default="aes-256-gcm-v1")
    key_version = models.CharField(max_length=32)
    wrapped_dek = models.BinaryField()
    wrap_nonce = models.BinaryField()
    data_nonce = models.BinaryField()
    ciphertext = models.BinaryField()
    status = models.CharField(max_length=24, choices=STATUS, default="active",
                              db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"MLSession<{self.organization_id}>"


class WhatsAppConnection(models.Model):
    """Mapeia uma única instância Node a uma organização."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="whatsapp_connection",
    )
    instance_id = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=24, default="inactive", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WA<{self.organization_id}:{self.instance_id}>"


def ensure_personal_organization(user) -> Organization:
    """Retorna/cria o tenant pessoal de um usuário de forma idempotente."""
    existing = Organization.objects.filter(personal_owner=user).first()
    if existing:
        Membership.objects.get_or_create(
            organization=existing, user=user,
            defaults={"role": "owner", "is_active": True},
        )
        WhatsAppConnection.objects.get_or_create(
            organization=existing,
            defaults={
                "instance_id": (
                    getattr(getattr(user, "perfil", None), "wa_session", "")
                    or str(user.pk)
                ),
            },
        )
        return existing

    base = slugify(user.get_username())[:80] or "conta"
    organization = Organization.objects.create(
        name=user.get_full_name() or user.get_username() or f"Conta {user.pk}",
        slug=f"{base}-{uuid.uuid4().hex[:10]}",
        personal_owner=user,
    )
    Membership.objects.create(
        organization=organization, user=user, role="owner", is_active=True,
    )
    WhatsAppConnection.objects.create(
        organization=organization, instance_id=str(user.pk),
    )
    return organization


def organization_for_user(user) -> Organization | None:
    """Resolve o tenant ativo sem confiar em um id vindo do cliente."""
    if not user or not getattr(user, "is_authenticated", False):
        return None
    personal = Organization.objects.filter(
        personal_owner=user, status="active",
    ).first()
    if personal:
        return personal
    membership = (
        Membership.objects.select_related("organization")
        .filter(user=user, is_active=True, organization__status="active")
        .order_by("created_at")
        .first()
    )
    return membership.organization if membership else None


class Perfil(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="perfil")
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="profile_settings",
    )

    # ── Verificação de e-mail ──
    email_verificado = models.BooleanField(default=False)
    verificado_em = models.DateTimeField(null=True, blank=True)

    # ── Tags de afiliado por usuário (cada um recebe a própria comissão) ──
    # Vazio = cai no fallback global de settings (AFILIADO_TAG / AMAZON_PARTNER_TAG).
    afiliado_tag_ml = models.CharField(max_length=120, blank=True, default="")
    afiliado_tag_amazon = models.CharField(max_length=120, blank=True, default="")

    # ── Conexão Amazon Creators API POR usuário (cada um usa a própria conta) ──
    # Vazio = cai no fallback global de settings. O secret é criptografado em repouso
    # (EncryptedCharField/Fernet, chave em SECRETS_FERNET_KEY). Coluna larga p/ o ciphertext.
    amazon_credential_id = models.CharField(max_length=255, blank=True, default="")
    amazon_credential_secret = EncryptedCharField(max_length=512, blank=True, default="")
    amazon_creators_host = models.CharField(max_length=255, blank=True, default="")
    # Última verificação de elegibilidade da conta Amazon (10 vendas/30d p/ a Creators
    # API). None = nunca raspado; False = 403 não-elegível; True = raspou ok. Exposto no
    # painel p/ o usuário entender por que a Amazon não gera itens (antes era silencioso).
    amazon_elegivel = models.BooleanField(null=True, blank=True)
    amazon_ultimo_erro = models.CharField(max_length=255, blank=True, default="")

    def amazon_conectado(self) -> bool:
        """True se a Amazon está habilitada para este usuário — ou seja, se ele tem tag.

        A tag é tudo o que a Amazon exige: o link comissionado é montado em Python
        puro (`?tag=`, scraper_amazon/link.py) e os dados de oferta vêm do fallback
        público quando não há Creators API. Com a tag, a Amazon funciona ponta a ponta.

        Já exigiu também credential_id + secret, e isso era um beco sem saída: as
        credenciais da Creators API só saem para contas com 10 vendas qualificadas em
        30 dias. O app pedia para "conectar a loja para gerar links comissionados" e,
        ao mesmo tempo, tornava a conexão inalcançável para quem ainda não vendeu —
        exatamente quem precisa da ferramenta. Quem tinha a tag via "Não conectada" e
        o alerta "Loja desconectada" para sempre, com a Amazon funcionando.

        A Creators API é um upgrade opcional: veja amazon_creators_ativa().
        """
        return bool(self.afiliado_tag_amazon)

    def amazon_creators_ativa(self) -> bool:
        """True se o usuário tem credenciais próprias da Creators API (upgrade opcional).

        Ortogonal a amazon_conectado(): melhora a origem dos dados de oferta (API oficial
        em vez do fallback público), não a capacidade de gerar link comissionado.
        Host NÃO é exigido: é fixo global (creators_api.DATA_HOST = creatorsapi.amazon);
        o campo amazon_creators_host só serve p/ override de dev.
        """
        return bool(self.amazon_credential_id and self.amazon_credential_secret)

    # ── Telegram por usuário (cada um conecta o próprio bot do @BotFather) ──
    # Vazio = cai no fallback global de settings (TELEGRAM_BOT_TOKEN). Criptografado
    # em repouso (Fernet), igual ao secret da Amazon. Coluna larga p/ o ciphertext;
    # tokens legados em texto puro seguem legíveis (decrypt devolve o próprio valor).
    telegram_bot_token = EncryptedCharField(max_length=512, blank=True, default="")

    def telegram_conectado(self) -> bool:
        """True se o usuário tem um bot do Telegram próprio configurado."""
        return bool(self.telegram_bot_token)

    # ── WhatsApp por usuário ──
    # Identificador da sessão (clientId) no serviço Node multi-cliente. Default = user.id.
    wa_session = models.CharField(max_length=64, blank=True, default="")

    # ── Último estado conhecido das conexões + carimbo do último alerta (anti-flood) ──
    wa_estado = models.BooleanField(null=True, blank=True)
    ml_estado = models.BooleanField(null=True, blank=True)
    alerta_wa_em = models.DateTimeField(null=True, blank=True)
    alerta_ml_em = models.DateTimeField(null=True, blank=True)
    # Sessão do portal Amazon Associates (relatórios), distinta da tag/Creators API.
    amazon_relatorio_estado = models.BooleanField(null=True, blank=True)
    alerta_amazon_relatorio_em = models.DateTimeField(null=True, blank=True)

    # ── Suspensão pelo superadmin ──
    # Conta bloqueada não loga (middleware) e é ignorada pelos loops de envio/scrape.
    bloqueado = models.BooleanField(default=False)
    bloqueado_em = models.DateTimeField(null=True, blank=True)
    bloqueado_motivo = models.CharField(max_length=255, blank=True, default="")

    # ── Cotas por usuário (0 = cai no default global de settings) ──
    # Evita que um usuário sozinho estoure a máquina compartilhada (WA ~3-4 sessões/2GB).
    max_wa_sessions = models.PositiveIntegerField(default=0)
    max_configs = models.PositiveIntegerField(default=0)
    max_envios_dia = models.PositiveIntegerField(default=0)

    # Identidade editorial usada em todas as mensagens.
    nome_marca = models.CharField(max_length=80, default="Ofertas")
    tom_marca = models.CharField(max_length=20, default="direto")
    nivel_emoji = models.PositiveSmallIntegerField(default=2)
    chamada_acao = models.CharField(max_length=120, default="Compre aqui")
    divulgacao_afiliado = models.CharField(max_length=180, blank=True, default="")
    template_a = models.TextField(blank=True, default="")
    template_b = models.TextField(blank=True, default="")

    # Entitlements simples para os pilotos; IDs externos permitem plugar Checkout
    # sem acoplar o acesso do produto ao provedor de cobrança.
    plano = models.CharField(max_length=20, default="piloto")
    assinatura_status = models.CharField(max_length=20, default="trial")
    trial_termina_em = models.DateTimeField(null=True, blank=True)
    billing_customer_id = models.CharField(max_length=120, blank=True, default="")
    billing_subscription_id = models.CharField(max_length=120, blank=True, default="")

    # ── Cotas: leitura com fallback pro default global ──
    def cota_max_configs(self) -> int:
        return self.max_configs or settings.QUOTA_MAX_CONFIGS

    def cota_max_envios_dia(self) -> int:
        return self.max_envios_dia or settings.QUOTA_MAX_ENVIOS_DIA

    def cota_max_wa_sessions(self) -> int:
        return self.max_wa_sessions or settings.QUOTA_MAX_WA_SESSIONS

    def marcar_bloqueado(self, motivo: str = ""):
        self.bloqueado = True
        self.bloqueado_em = timezone.now()
        self.bloqueado_motivo = motivo
        self.save(update_fields=["bloqueado", "bloqueado_em", "bloqueado_motivo"])

    def desbloquear(self):
        self.bloqueado = False
        self.bloqueado_em = None
        self.bloqueado_motivo = ""
        self.save(update_fields=["bloqueado", "bloqueado_em", "bloqueado_motivo"])

    def marcar_verificado(self):
        self.email_verificado = True
        self.verificado_em = timezone.now()
        self.save(update_fields=["email_verificado", "verificado_em"])

    def sessao_whatsapp(self) -> str:
        """Sessão WA do usuário (default = id do usuário em string)."""
        organization = organization_for_user(self.user)
        if organization is not None:
            connection = WhatsAppConnection.objects.filter(
                organization=organization,
            ).first()
            if connection:
                return connection.instance_id
        return self.wa_session or str(self.user_id)

    def __str__(self):
        return f"Perfil<{self.user.get_username()}>"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def criar_perfil(sender, instance, created, **kwargs):
    """Todo User ganha um Perfil. Superusuário (createsuperuser) já nasce verificado."""
    organization = ensure_personal_organization(instance)
    if created:
        Perfil.objects.create(
            user=instance,
            organization=organization,
            email_verificado=bool(instance.is_superuser),
            verificado_em=timezone.now() if instance.is_superuser else None,
        )
    else:
        # Garante perfil p/ usuários antigos criados antes do model existir.
        profile, _ = Perfil.objects.get_or_create(
            user=instance,
            defaults={"organization": organization},
        )
        if profile.organization_id != organization.pk:
            profile.organization = organization
            profile.save(update_fields=["organization"])
