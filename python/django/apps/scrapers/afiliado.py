"""Resolução de identidade de afiliado + cache de link POR usuário.

No Mercado Livre a identidade vem exclusivamente da conta autenticada no Link
Builder. A tag textual só existe no fluxo Amazon.
"""
from django.conf import settings


def tag_ml(usuario=None) -> str:
    """Compatibilidade: ML não possui tag manual separada neste fluxo."""
    return ""


def tag_amazon(usuario=None) -> str:
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.afiliado_tag_amazon:
            return perfil.afiliado_tag_amazon.strip()
        return ""
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
