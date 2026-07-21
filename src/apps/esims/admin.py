"""Admin registration for esims."""

from django.contrib import admin

from apps.esims.models import Esim


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    list_display = ("id", "iccid", "user", "order", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("iccid", "user__email", "matching_id")
    readonly_fields = ("created_at", "updated_at", "usage_synced_at")
    raw_id_fields = ("user", "order")
