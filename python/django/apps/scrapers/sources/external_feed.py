import hashlib
import requests
from django.conf import settings
from django.utils import timezone
from .base import IngestedItem, SourceAdapter


class LicensedFeedSource(SourceAdapter):
    """Conector JSON genérico para uma rede licenciada; inativo sem URL explícita."""
    slug = "licensed-affiliate-feed"
    marketplace = "multiloja"
    name = "Feed licenciado de afiliados"

    def _rows(self):
        url = getattr(settings, "AFFILIATE_FEED_URL", "")
        if not url:
            return []
        headers = {}
        token = getattr(settings, "AFFILIATE_FEED_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        return response.json().get("items", response.json())

    def discover_offers(self, **kwargs):
        for row in self._rows():
            url = row.get("url", "")
            price, before = float(row.get("price") or 0), float(row.get("before") or 0)
            if not url or price <= 0 or before <= price:
                continue
            yield IngestedItem(
                external_id=str(row.get("id") or hashlib.sha256(url.encode()).hexdigest()),
                marketplace=row.get("marketplace", "multiloja"), source=self.slug,
                kind="offer", canonical_url=url, title=row.get("title", "")[:255],
                current_price=price, reference_price=before, observed_at=timezone.now(),
                evidence={"transport": "licensed-json-feed"},
            )

    def discover_coupons(self, **kwargs):
        return []
