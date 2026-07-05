"""Perfil do usuário — extensão 1:1 do User para o SaaS multi-tenant.

Guarda: estado de verificação de e-mail, tags de afiliado por usuário (ML + Amazon),
sessão de WhatsApp do usuário e o último estado conhecido das conexões (p/ alertas).
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from apps.accounts.fields import EncryptedCharField


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
        """True se o usuário tem credenciais Amazon próprias completas.
        Host NÃO é exigido: é fixo global (creators_api.DATA_HOST = creatorsapi.amazon);
        o campo amazon_creators_host só serve p/ override de dev."""
        return bool(self.amazon_credential_id and self.amazon_credential_secret
                    and self.afiliado_tag_amazon)

    # ── Telegram por usuário (cada um conecta o próprio bot do @BotFather) ──
    # Vazio = cai no fallback global de settings (TELEGRAM_BOT_TOKEN). Texto puro
    # como as credenciais Amazon acima; criptografar quando virar multi-host.
    telegram_bot_token = models.CharField(max_length=120, blank=True, default="")

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
