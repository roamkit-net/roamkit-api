"""Admin registration for billing (list/filter/search; no money actions)."""

from django.contrib import admin

from apps.billing.models import (
    Account,
    CreditLedgerEntry,
    DepositRequest,
    Subscription,
)


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "balance", "version", "created_at")
    search_fields = ("user__email", "id")
    readonly_fields = (
        "id",
        "balance",
        "version",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("user",)


@admin.register(DepositRequest)
class DepositRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "amount_requested",
        "amount_credited",
        "payment_method",
        "status",
        "tx_hash",
        "created_at",
    )
    list_filter = ("status", "payment_method")
    search_fields = (
        "idempotency_key",
        "tx_hash",
        "account__user__email",
        "id",
    )
    readonly_fields = ("id", "created_at", "updated_at", "verified_at")
    raw_id_fields = ("account",)


@admin.register(CreditLedgerEntry)
class CreditLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "delta",
        "balance_after",
        "reference_type",
        "reference_id",
        "created_at",
    )
    list_filter = ("reference_type",)
    search_fields = (
        "idempotency_key",
        "reference_id",
        "account__user__email",
        "id",
    )
    readonly_fields = (
        "id",
        "account",
        "delta",
        "balance_after",
        "reference_type",
        "reference_id",
        "idempotency_key",
        "created_at",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "esim",
        "price_per_period",
        "next_billing_date",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = ("account__user__email", "esim__iccid", "id")
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("account", "esim")
