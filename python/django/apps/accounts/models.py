"""Perfil do usuário — extensão 1:1 do User para o SaaS multi-tenant.

Guarda: estado de verificação de e-mail, tags de afiliado por usuário (ML + Amazon),
sessão de WhatsApp do usuário e o último estado conhecido das conexões (p/ alertas).
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


class Perfil(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="perfil")

    # ── Verificação de e-mail ──
    email_verificado = models.BooleanField(default=False)
    verificado_em = models.DateTimeField(null=True, blank=True)

    # ── Tags de afiliado por usuário (cada um recebe a própria comissão) ──
    # Vazio = cai no fallback global de settings (AFILIADO_TAG / AMAZON_PARTNER_TAG).
    afiliado_tag_ml = models.CharField(max_length=120, blank=True, default="")
    afiliado_tag_amazon = models.CharField(max_length=120, blank=True, default="")

    # ── Conexão Amazon Creators API POR usuário (cada um usa a própria conta) ──
    # Vazio = cai no fallback global de settings. O secret fica em texto (single-box);
    # criptografar quando virar multi-host (django-fernet-fields/KMS) — ver roadmap.
    amazon_credential_id = models.CharField(max_length=255, blank=True, default="")
    amazon_credential_secret = models.CharField(max_length=255, blank=True, default="")
    amazon_creators_host = models.CharField(max_length=255, blank=True, default="")

    def amazon_conectado(self) -> bool:
        """True se o usuário tem credenciais Amazon próprias completas."""
        return bool(self.amazon_credential_id and self.amazon_credential_secret
                    and self.amazon_creators_host and self.afiliado_tag_amazon)

    # ── WhatsApp por usuário ──
    # Identificador da sessão (clientId) no serviço Node multi-cliente. Default = user.id.
    wa_session = models.CharField(max_length=64, blank=True, default="")

    # ── Último estado conhecido das conexões + carimbo do último alerta (anti-flood) ──
    wa_estado = models.BooleanField(null=True, blank=True)
    ml_estado = models.BooleanField(null=True, blank=True)
    alerta_wa_em = models.DateTimeField(null=True, blank=True)
    alerta_ml_em = models.DateTimeField(null=True, blank=True)

    def marcar_verificado(self):
        self.email_verificado = True
        self.verificado_em = timezone.now()
        self.save(update_fields=["email_verificado", "verificado_em"])

    def sessao_whatsapp(self) -> str:
        """Sessão WA do usuário (default = id do usuário em string)."""
        return self.wa_session or str(self.user_id)

    def __str__(self):
        return f"Perfil<{self.user.get_username()}>"


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def criar_perfil(sender, instance, created, **kwargs):
    """Todo User ganha um Perfil. Superusuário (createsuperuser) já nasce verificado."""
    if created:
        Perfil.objects.create(
            user=instance,
            email_verificado=bool(instance.is_superuser),
            verificado_em=timezone.now() if instance.is_superuser else None,
        )
    else:
        # Garante perfil p/ usuários antigos criados antes do model existir.
        Perfil.objects.get_or_create(user=instance)
