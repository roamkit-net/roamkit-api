"""Admin for Organization / Membership — schema visibility only (ADR 020 PR1)."""

from __future__ import annotations

from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.billing.services.partner_self_referral import (
    PartnerSelfReferralConflict,
    ensure_active_membership_allowed,
)
from apps.organizations.models import (
    DeviceBinding,
    DeviceBindingEvent,
    FleetCredentialEvent,
    Membership,
    MembershipStatus,
    Organization,
    OrganizationFleetCredential,
    OrganizationInvite,
)
from apps.organizations.services.account_binding import create_organization

User = get_user_model()


class OrganizationAdminForm(forms.ModelForm):
    """Add asks for an owner. The team Account is created with the organization."""

    owner = forms.ModelChoiceField(
        queryset=User.objects.order_by("id"),
        required=False,
        help_text=(
            "Required for a new organization. This person becomes the owner. "
            "Their personal account is not converted."
        ),
    )

    class Meta:
        model = Organization
        fields = ("name", "status")

    def clean(self):
        cleaned = super().clean()
        if self.instance._state.adding and cleaned.get("owner") is None:
            self.add_error("owner", "Choose the owner of the new organization.")
        return cleaned


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    can_delete = False
    raw_id_fields = ("user",)
    fields = ("id", "user", "role", "status", "created_at", "updated_at")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    """Add creates the organization, its team account, and the owner membership.

    Change edits name and status. The team account stays bound and is not a
    personal account.
    """

    form = OrganizationAdminForm
    list_display = ("name", "status", "account", "created_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("name", "id", "account__id")
    inlines = (MembershipInline,)

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("name", "status", "owner")
        return ("name", "status", "id", "account", "created_at", "updated_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return ("id", "account", "created_at", "updated_at")

    def save_model(self, request, obj, form, change) -> None:
        if change:
            obj.save(update_fields=["name", "status", "updated_at"])
            return
        org = create_organization(
            name=form.cleaned_data["name"],
            actor=form.cleaned_data["owner"],
            status=form.cleaned_data["status"],
        )
        obj.pk = org.pk
        obj.account_id = org.account_id

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

    def save_model(self, request, obj, form, change) -> None:
        if obj.status != MembershipStatus.ACTIVE:
            super().save_model(request, obj, form, change)
            return
        with transaction.atomic():
            try:
                ensure_active_membership_allowed(
                    user=obj.user,
                    organization=obj.organization,
                )
            except PartnerSelfReferralConflict as exc:
                raise ValidationError(str(exc)) from exc
            super().save_model(request, obj, form, change)

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
        "uem_serial_number",
        "uem_device_guid",
        "status",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "id",
        "device_external_id",
        "uem_serial_number",
        "uem_device_guid",
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
    # Serial/guid editable for staging ADR 021 Option C′ ops map.
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


@admin.register(OrganizationFleetCredential)
class OrganizationFleetCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "fleet_external_id",
        "organization",
        "current_issued_at",
        "previous_valid_until",
        "created_at",
    )
    search_fields = (
        "fleet_external_id",
        "organization__name",
        "organization__id",
    )
    raw_id_fields = ("organization",)
    readonly_fields = (
        "id",
        "fleet_external_id",
        "current_credential_hash",
        "current_issued_at",
        "previous_credential_hash",
        "previous_valid_until",
        "created_at",
        "updated_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(FleetCredentialEvent)
class FleetCredentialEventAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "organization",
        "action",
        "fleet_external_id",
        "actor",
        "previous_valid_until",
        "created_at",
    )
    list_filter = ("action",)
    search_fields = (
        "id",
        "fleet_external_id",
        "organization__id",
    )
    raw_id_fields = ("organization", "fleet_credential", "actor")
    readonly_fields = (
        "id",
        "organization",
        "fleet_credential",
        "action",
        "actor",
        "fleet_external_id",
        "previous_valid_until",
        "created_at",
    )

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def has_add_permission(self, request) -> bool:
        return False
