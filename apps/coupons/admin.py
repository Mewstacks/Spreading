from django.contrib import admin

from .models import Coupon


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    list_display = ("code", "store", "discount_pct", "max_discount", "valid_until", "last_seen")
    search_fields = ("code",)
