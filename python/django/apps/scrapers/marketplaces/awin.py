"""Awin — produtos ja chegam com deep link comissionado no feed do publisher."""
from apps.scrapers.marketplaces.base import Marketplace


class Awin(Marketplace):
    slug = "awin"

    def scrape_all(self, termos=None):
        # A sincronizacao e por IntegracaoAfiliado e ja roda no worker dedicado.
        return None

    def build_affiliate_link(self, produto, usuario=None):
        link = str(getattr(produto, "link_produto", "") or "").strip()
        if not link:
            return None
        return {"link_afiliado": link, "afiliado_ok": True, "url_isca": link}

    def verify_affiliate_tag(self, link, usuario=None):
        return str(link or "").startswith(("https://www.awin1.com/", "http://www.awin1.com/",
                                           "https://awin1.com/", "http://awin1.com/"))

    def can_affiliate(self, produto, usuario=None):
        return bool(getattr(produto, "owner_id", None) == getattr(usuario, "id", None)
                    and self.verify_affiliate_tag(getattr(produto, "link_produto", "")))
