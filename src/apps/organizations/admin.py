"""Admin for Organization / Membership — schema visibility only (ADR 020 PR1)."""

from __future__ import annotations

from django.contrib import admin

from apps.organizations.models import (
    DeviceBinding,
    DeviceBindingEvent,
    Membership,
    Organization,
    OrganizationInvite,
)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    can_delete = False
    raw_id_fields = ("user",)
    fields = ("id", "user", "role", "status", "created_at", "updated_at")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "account", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "id", "account__id")
    readonly_fields = ("id", "account", "created_at", "updated_at")
    raw_id_fields = ("account",)
    inlines = (MembershipInline,)

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_add_permission(self, request) -> bool:
        # Create via create_organization() so team Account is always bound.
        return False


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "user",
        "role",
        "status",
        "created_at",
    )
    list_filter = ("role", "status")
    search_fields = (
        "id",
        "organization__name",
        "organization__id",
        "user__email",
    )
    raw_id_fields = ("organization", "user")
    readonly_fields = ("id", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(OrganizationInvite)
class OrganizationInviteAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "email_normalized",
        "role",
        "status",
        "expires_at",
        "created_at",
    )
    list_filter = ("status", "role")
    search_fields = (
        "id",
        "email",
        "email_normalized",
        "organization__name",
        "organization__id",
    )
    raw_id_fields = ("organization", "invited_by", "accepted_by")
    readonly_fields = (
        "id",
        "token_hash",
        "accepted_at",
        "revoked_at",
        "created_at",
        "updated_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(DeviceBinding)
class DeviceBindingAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "esim",
        "device_external_id",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "id",
        "device_external_id",
        "organization__name",
        "organization__id",
        "esim__iccid",
    )
    raw_id_fields = (
        "organization",
        "esim",
        "bound_by",
        "unbound_by",
        "replaced_by",
    )
    readonly_fields = (
        "id",
        "device_external_id",
        "credential_hash",
        "credential_issued_at",
        "created_at",
        "updated_at",
        "unbound_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(DeviceBindingEvent)
class DeviceBindingEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "action",
        "device_external_id",
        "actor",
        "created_at",
    )
    list_filter = ("action",)
    search_fields = (
        "id",
        "device_external_id",
        "organization__id",
        "binding__id",
    )
    raw_id_fields = (
        "organization",
        "binding",
        "esim",
        "actor",
        "previous_binding",
    )
    readonly_fields = (
        "id",
        "organization",
        "binding",
        "esim",
        "action",
        "actor",
        "device_external_id",
        "previous_binding",
        "created_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_add_permission(self, request) -> bool:
        return False
