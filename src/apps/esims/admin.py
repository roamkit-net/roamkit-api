"""Admin registration for esims."""

from django.contrib import admin

from apps.esims.models import Esim, Topup


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    list_display = ("id", "iccid", "user", "order", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("iccid", "user__email", "matching_id")
    readonly_fields = ("created_at", "updated_at", "usage_synced_at")
    raw_id_fields = ("user", "order")


@admin.register(Topup)
class TopupAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "esim",
        "package_external_id",
        "amount",
        "status",
        "external_order_id",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "id",
        "package_external_id",
        "external_order_id",
        "esim__iccid",
        "account__user__email",
    )
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("account", "esim")
