"""Admin registration for esims."""

from django.contrib import admin

from apps.esims.models import Esim, EsimLifecycleEvent, Topup


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "iccid",
        "user",
        "order",
        "status",
        "activation_policy",
        "note_preview",
        "created_at",
    )
    list_filter = ("status", "activation_policy")
    search_fields = ("iccid", "user__email", "matching_id", "note")
    readonly_fields = (
        "created_at",
        "updated_at",
        "usage_synced_at",
        "setup_completed_at",
        "setup_skipped_at",
    )
    raw_id_fields = ("user", "order")

    @admin.display(description="Note", ordering="note")
    def note_preview(self, obj: Esim) -> str:
        note = obj.note or ""
        if len(note) <= 40:
            return note
        return f"{note[:40]}…"


@admin.register(EsimLifecycleEvent)
class EsimLifecycleEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "esim",
        "event_type",
        "source",
        "setup_session_id",
        "created_at",
    )
    list_filter = ("source", "event_type")
    search_fields = ("idempotency_key", "esim__iccid", "user__email")
    readonly_fields = (
        "id",
        "esim",
        "user",
        "event_type",
        "source",
        "schema_version",
        "idempotency_key",
        "setup_session_id",
        "payload",
        "user_agent",
        "created_at",
    )
    raw_id_fields = ("esim", "user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


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
