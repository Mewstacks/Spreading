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

    def prefetch_links(self, produtos, usuario=None, faixa=None):
        """Valida e materializa o deep link Awin no cache uniforme por usuário."""
        from apps.scrapers.afiliado import registrar_falha, salvar_cache

        gerados = falhas = 0
        for produto in produtos:
            link = str(getattr(produto, "link_produto", "") or "").strip()
            if (getattr(produto, "owner_id", None) != getattr(usuario, "id", None)
                    or not self.verify_affiliate_tag(link, usuario=usuario)):
                registrar_falha(
                    usuario, produto,
                    "O feed não forneceu um deep link Awin válido para esta conta.",
                    terminal=True,
                )
                falhas += 1
                continue
            salvar_cache(
                usuario, produto, link, link, True,
                verificado_ok=True, url_canonica=link,
                verificacao_motivo="deep link Awin validado",
            )
            gerados += 1
        return gerados, falhas
