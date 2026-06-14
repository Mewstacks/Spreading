"""Resolução de tag de afiliado + cache de link POR usuário (multi-tenant).

Cada usuário recebe a própria comissão, então o link precisa carregar a tag DELE.
Tag por usuário vem do Perfil; se vazia, cai no fallback global de settings (compat
single-tenant). O cache de link fica em LinkAfiliadoUsuario keyed (usuario, produto).
"""
from django.conf import settings


def tag_ml(usuario=None) -> str:
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.afiliado_tag_ml:
            return perfil.afiliado_tag_ml.strip()
    return (getattr(settings, "AFILIADO_TAG", "") or "").strip()


def tag_amazon(usuario=None) -> str:
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.afiliado_tag_amazon:
            return perfil.afiliado_tag_amazon.strip()
    return (getattr(settings, "AMAZON_PARTNER_TAG", "") or "").strip()


def link_cacheado(usuario, produto):
    """Retorna o LinkAfiliadoUsuario (usuario, produto) ou None."""
    if usuario is None:
        return None
    from apps.scrapers.models import LinkAfiliadoUsuario
    return LinkAfiliadoUsuario.objects.filter(usuario=usuario, produto=produto).first()


def salvar_cache(usuario, produto, link_afiliado, url_isca, afiliado_ok) -> None:
    if usuario is None or not link_afiliado:
        return
    from apps.scrapers.models import LinkAfiliadoUsuario
    LinkAfiliadoUsuario.objects.update_or_create(
        usuario=usuario, produto=produto,
        defaults={"link_afiliado": link_afiliado, "url_isca": url_isca,
                  "afiliado_ok": afiliado_ok},
    )
