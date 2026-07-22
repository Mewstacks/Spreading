from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True)
class IngestedItem:
    external_id: str
    marketplace: str
    source: str
    kind: str
    canonical_url: str
    title: str
    current_price: float = 0
    reference_price: float = 0
    coupon_code: str = ""
    coupon_rules: dict[str, Any] = field(default_factory=dict)
    content_type: str = "voucher"
    starts_at: datetime | None = None
    restricted: bool = False
    flash: bool = False
    valid_until: datetime | None = None
    observed_at: datetime | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class SourceAdapter:
    slug = ""
    marketplace = ""
    name = ""

    def discover_offers(self, **kwargs) -> Iterable[IngestedItem]:
        return []

    def discover_coupons(self, **kwargs) -> Iterable[IngestedItem]:
        return []

    def refresh_offer(self, item: IngestedItem, **kwargs) -> IngestedItem | None:
        raise NotImplementedError

    def healthcheck(self) -> dict:
        return {"ok": True}
