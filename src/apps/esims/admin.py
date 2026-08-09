"""Admin registration for esims."""

from django.contrib import admin

from apps.esims.models import Esim, EsimAutoTopupPolicy, EsimLifecycleEvent, Topup


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "iccid",
        "account",
        "user",
        "assigned_user",
        "order",
        "status",
        "activation_policy",
        "note_preview",
        "archived_at",
        "created_at",
    )
    list_filter = ("status", "activation_policy", "archived_at")
    search_fields = (
        "iccid",
        "user__email",
        "assigned_user__email",
        "account__id",
        "matching_id",
        "note",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "usage_synced_at",
        "setup_completed_at",
        "setup_skipped_at",
        "archived_at",
    )
    raw_id_fields = ("account", "user", "assigned_user", "order")

    @admin.display(description="Note", ordering="note")
    def note_preview(self, obj: Esim) -> str:
        note = obj.note or ""
        if len(note) <= 40:
            return note
        return f"{note[:40]}…"

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


@admin.register(EsimAutoTopupPolicy)
class EsimAutoTopupPolicyAdmin(admin.ModelAdmin):
    """Support-oriented view; money still mutates only via CreditService."""

    list_display = (
        "id",
        "esim",
        "account",
        "package_id",
        "enabled",
        "status",
        "reason",
        "expiry_enabled",
        "usage_mode",
        "threshold_mb",
        "renew_mode",
        "active_until",
        "cooldown_until",
        "last_triggered_at",
        "version",
        "updated_at",
    )
    list_filter = (
        "status",
        "enabled",
        "expiry_enabled",
        "usage_mode",
        "renew_mode",
        "reason",
    )
    search_fields = (
        "id",
        "package_id",
        "last_idempotency_key",
        "esim__iccid",
        "account__user__email",
    )
    readonly_fields = (
        "id",
        "last_triggered_at",
        "last_topup",
        "last_idempotency_key",
        "cooldown_until",
        "version",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("account", "esim", "last_topup")
