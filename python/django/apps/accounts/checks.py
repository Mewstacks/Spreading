"""Checks de implantação que impedem regressões fail-open."""

import base64

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


@register(Tags.security, deploy=True)
def phase0_security_checks(app_configs, **kwargs):
    if settings.APP_ENV not in {"staging", "production"}:
        return []

    errors = []
    if settings.PHASE0_EXPAND_ONLY:
        errors.append(Warning(
            "PHASE0_EXPAND_ONLY está ativo; finalize constraints/RLS e remova "
            "o secret antes de encerrar a janela.",
            id="accounts.W001",
        ))
    if settings.DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        errors.append(Error(
            "Staging/produção exige PostgreSQL.",
            id="accounts.E001",
        ))
    if not settings.SECURITY_FREEZE_NEW_TENANTS:
        errors.append(Error(
            "O freeze de novos tenants deve permanecer ativo durante a Fase 0.",
            id="accounts.E002",
        ))
    if settings.ML_LEGACY_SESSION_READ_ENABLED:
        errors.append(Error(
            "Leitura de sessão ML em plaintext não pode estar ativa.",
            id="accounts.E003",
        ))
    if not settings.MIGRATION_DATABASE_CONFIGURED:
        errors.append(Error(
            "MIGRATION_DATABASE_URL é obrigatória para DDL isolado do runtime.",
            id="accounts.E007",
        ))
    if not settings.SYSTEM_DATABASE_CONFIGURED:
        errors.append(Error(
            "SYSTEM_DATABASE_URL é obrigatória para workers cross-tenant.",
            id="accounts.E008",
        ))
    if settings.PERMITIR_CADASTRO_PUBLICO:
        errors.append(Error(
            "Cadastro público deve permanecer fechado durante a Fase 0.",
            id="accounts.E009",
        ))
    fragile_flags = (
        settings.ML_BROWSER_LOGIN_ENABLED,
        settings.ML_LINK_BUILDER_ENABLED,
        settings.ML_BROWSER_REPORTS_ENABLED,
        settings.WHATSAPP_WEB_ENABLED,
    )
    if any(fragile_flags) and not settings.PILOT_ORGANIZATION_IDS:
        errors.append(Error(
            "Automação frágil ativa exige PILOT_ORGANIZATION_IDS não vazio.",
            id="accounts.E010",
        ))
    if settings.TELETHON_RELINK_ENABLED:
        errors.append(Error(
            "Telethon/relink deve permanecer desligado nesta fase.",
            id="accounts.E011",
        ))
    # Avisos de configuração que quebram funcionalidades EM SILÊNCIO. Nenhum deles
    # gera erro em log nem mensagem de tela: o usuário só vê "0 cupons" ou "sessão
    # expirada" e vai reconectar o Mercado Livre, que não é o problema.
    if not settings.ML_SYSTEM_ORGANIZATION_ID:
        errors.append(Warning(
            "ML_SYSTEM_ORGANIZATION_ID vazio: o preparo de cupons PÚBLICOS do "
            "Mercado Livre roda anônimo (scrapers/coupon_products.py resolve o "
            "contexto como None para eles) e os cupons nunca ficam prontos, mesmo "
            "com todos os usuários conectados.",
            id="accounts.W002",
        ))
    if not settings.ML_LINK_BUILDER_ENABLED:
        errors.append(Warning(
            "ML_LINK_BUILDER_ENABLED desligada: nenhum link de afiliado do Mercado "
            "Livre será gerado. A tela de Promoções mostrará tudo como pendente.",
            id="accounts.W003",
        ))
    if not settings.ML_BROWSER_LOGIN_ENABLED:
        errors.append(Warning(
            "ML_BROWSER_LOGIN_ENABLED desligada: a tela de Conexão Mercado Livre "
            "não consegue abrir o login.",
            id="accounts.W004",
        ))
    try:
        tenant_key = settings.TENANT_CONTEXT_SIGNING_KEY
        padded_key = tenant_key + ("=" * (-len(tenant_key) % 4))
        decoded_key = base64.b64decode(
            padded_key, altchars=b"-_", validate=True,
        )
        if len(decoded_key) < 32:
            raise ValueError("menos de 256 bits")
    except Exception as exc:
        errors.append(Error(
            f"TENANT_CONTEXT_SIGNING_KEY inválida: {exc}",
            id="accounts.E012",
        ))

    try:
        from .ml_session_crypto import current_key_version
        current_key_version()
    except Exception as exc:
        errors.append(Error(
            f"Keyring de sessões ML inválido: {exc}",
            id="accounts.E005",
        ))

    try:
        from .wa_capabilities import public_key_base64url
        public_key_base64url()
    except Exception as exc:
        errors.append(Error(
            f"Chave Ed25519 do WhatsApp inválida: {exc}",
            id="accounts.E006",
        ))

    return errors
