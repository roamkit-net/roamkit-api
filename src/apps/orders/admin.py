"""Admin registration for orders."""

from django.contrib import admin

from apps.orders.models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "package_title",
        "retail_price_usd",
        "currency",
        "status",
        "external_order_id",
        "created_at",
    )
    list_filter = ("status", "currency", "country_code")
    search_fields = (
        "external_order_id",
        "customer_ref",
        "package_title",
        "account__user__email",
    )
    readonly_fields = (
        "package_title",
        "operator_title",
        "location_title",
        "country_code",
        "data_allowance",
        "validity_days",
        "retail_price_usd",
        "currency",
        "net_price_usd",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("account", "package")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "account",
                    "package",
                    "status",
                    "external_order_id",
                    "customer_ref",
                    "idempotency_key",
                )
            },
        ),
        (
            "Product snapshot (immutable)",
            {
                "fields": (
                    "package_title",
                    "operator_title",
                    "location_title",
                    "country_code",
                    "data_allowance",
                    "validity_days",
                    "retail_price_usd",
                    "currency",
                    "net_price_usd",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
