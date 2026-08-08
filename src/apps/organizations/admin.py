"""Admin for Organization / Membership — schema visibility only (ADR 020 PR1)."""

from __future__ import annotations

from django.contrib import admin

from apps.organizations.models import Membership, Organization


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    can_delete = False
    raw_id_fields = ("user",)
    fields = ("id", "user", "role", "status", "created_at", "updated_at")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "id")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = (MembershipInline,)

    def has_delete_permission(self, request, obj=None) -> bool:
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
