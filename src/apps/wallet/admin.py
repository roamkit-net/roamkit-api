"""Admin for wallet domain — read-focused; allocation is via service (next PR)."""

from __future__ import annotations

from django.contrib import admin

from apps.wallet.models import DepositObservation, WalletAddress, WalletIdentity


class WalletAddressInline(admin.TabularInline):
    model = WalletAddress
    extra = 0
    can_delete = False
    readonly_fields = (
        "id",
        "chain",
        "address",
        "derivation_index",
        "status",
        "created_at",
        "retired_at",
    )
    fields = readonly_fields

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(WalletIdentity)
class WalletIdentityAdmin(admin.ModelAdmin):
    list_display = ("id", "account", "created_at")
    search_fields = ("id", "account__id", "account__user__email")
    readonly_fields = ("id", "account", "created_at", "updated_at")
    raw_id_fields = ("account",)
    inlines = (WalletAddressInline,)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(WalletAddress)
class WalletAddressAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "wallet_identity",
        "chain",
        "address",
        "derivation_index",
        "status",
        "created_at",
        "retired_at",
    )
    list_filter = ("chain", "status")
    search_fields = ("address", "id", "wallet_identity__id")
    readonly_fields = (
        "id",
        "wallet_identity",
        "chain",
        "address",
        "derivation_index",
        "status",
        "created_at",
        "retired_at",
    )
    raw_id_fields = ("wallet_identity",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(DepositObservation)
class DepositObservationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "chain",
        "tx_hash",
        "log_index",
        "status",
        "amount",
        "wallet_address",
        "confirmations",
        "observed_at",
    )
    list_filter = ("chain", "status")
    search_fields = ("tx_hash", "id", "wallet_address__address")
    readonly_fields = (
        "id",
        "wallet_address",
        "chain",
        "tx_hash",
        "log_index",
        "status",
        "amount",
        "token_contract",
        "from_address",
        "confirmations",
        "block_number",
        "status_reason",
        "observed_at",
        "pending_at",
        "confirmed_at",
        "conversion_started_at",
        "credited_at",
        "rejected_at",
        "expired_at",
        "updated_at",
    )
    raw_id_fields = ("wallet_address",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
