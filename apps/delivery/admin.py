from django.contrib import admin

from .models import CouponDelivery, OutboxItem


@admin.register(OutboxItem)
class OutboxItemAdmin(admin.ModelAdmin):
    list_display = ("id", "destination", "state", "attempts", "created_at", "sent_at")
    list_filter = ("state",)


admin.site.register(CouponDelivery)
