"""Admin registration for orders."""

from django.contrib import admin

from apps.orders.models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "package",
        "status",
        "external_order_id",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("external_order_id", "customer_ref", "user__email")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("user", "package")
