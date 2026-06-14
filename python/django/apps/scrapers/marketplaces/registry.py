"""Registry de marketplaces. Adicionar loja = importar + uma entrada aqui."""
from apps.scrapers.marketplaces.mercadolivre import MercadoLivre
from apps.scrapers.marketplaces.amazon import Amazon

MARKETPLACES = {
    MercadoLivre.slug: MercadoLivre(),
    Amazon.slug: Amazon(),
    # FUTURO:
    # Shopee.slug: Shopee(),
}


def get_marketplace(slug: str):
    """Retorna o Marketplace pelo slug (default Mercado Livre)."""
    return MARKETPLACES.get((slug or "mercadolivre").lower(), MARKETPLACES["mercadolivre"])
