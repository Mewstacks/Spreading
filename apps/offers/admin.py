from django.contrib import admin

from .models import AffiliateLink, Offer, Rule


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "destination", "active", "interval_minutes", "daily_cap")


@admin.register(AffiliateLink)
class AffiliateLinkAdmin(admin.ModelAdmin):
    list_display = ("product_key", "affiliate_url", "etiqueta", "created_at")
    search_fields = ("product_key",)


@admin.register(Offer)
class OfferAdmin(admin.ModelAdmin):
    list_display = ("title", "price_to", "discount_pct", "valid_on", "source")
    search_fields = ("title",)
