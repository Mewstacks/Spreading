from .base import SourceAdapter


class CommunitySource(SourceAdapter):
    """Ponto de extensão inativo até revisão/autorização dos termos."""
    marketplace = "multiloja"

    def healthcheck(self):
        return {"ok": False, "status": "disabled",
                "reason": "Requer autorização e revisão dos termos da fonte."}


class PromobitSource(CommunitySource):
    slug, name = "promobit-community", "Promobit (experimental)"


class PelandoSource(CommunitySource):
    slug, name = "pelando-community", "Pelando (experimental)"
