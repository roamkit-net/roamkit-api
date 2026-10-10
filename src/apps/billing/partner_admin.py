"""Partner admin. Creation is admin-provisioned. Ownership is immutable."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Sum

from apps.accounts.models import User
from apps.billing.models import AccountKind
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
    PartnerInviteLink,
    PartnerMarginAccrual,
)
from apps.billing.services.partner_invite import (
    PartnerInviteError,
    canonical_invite_link,
    create_individual_partner_channel,
    create_partner_channel,
    persist_with_unique_invite_token,
    update_partner_channel_settings,
)
from apps.billing.services.partner_settlement import (
    PartnerChannelOwnershipInvalid,
    PartnerSettlementAccountMissing,
    resolve_partner_settlement_account,
)
from apps.organizations.models import Organization


class PartnerChannelAdminForm(forms.ModelForm):
    class Meta:
        model = PartnerChannel
        fields = (
            "kind",
            "owner_user",
            "organization",
            "is_active",
            "revenue_share_percent",
        )

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            return cleaned
        kind = cleaned.get("kind")
        owner = cleaned.get("owner_user")
        organization = cleaned.get("organization")
        if kind == PartnerChannel.Kind.INDIVIDUAL:
            if owner is None or organization is not None:
                raise ValidationError(
                    "An individual channel needs an owner and no organization."
                )
        elif kind == PartnerChannel.Kind.TEAM:
            if organization is None or owner is not None:
                raise ValidationError(
                    "A team channel needs an organization and no owner user."
                )
        else:
            raise ValidationError("Partner channel kind is required.")
        return cleaned


@admin.register(PartnerChannel)
class PartnerChannelAdmin(admin.ModelAdmin):
    form = PartnerChannelAdminForm
    list_display = (
        "kind",
        "owner_user",
        "organization",
        "is_active",
        "revenue_share_percent",
        "attribution_total",
        "accrual_total",
        "grant_total",
        "created_at",
    )
    list_filter = ("kind", "is_active")
    search_fields = (
        "owner_user__email",
        "owner_user__display_name",
        "organization__name",
    )
    readonly_fields = (
        "id",
        "kind",
        "owner_user",
        "organization",
        "created_at",
        "updated_at",
        "settlement_account_id",
        "invite_is_active",
        "attribution_count",
        "earned_sum",
        "grant_count",
    )
    fields = (
        "kind",
        "owner_user",
        "organization",
        "is_active",
        "revenue_share_percent",
        "settlement_account_id",
        "created_at",
        "updated_at",
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("owner_user", "organization")
            .annotate(
                attribution_total=Count("customer_attributions", distinct=True),
                accrual_total=Count("margin_accruals", distinct=True),
                grant_total=Count("credit_grants", distinct=True),
            )
        )

    @admin.display(description="Customers")
    def attribution_total(self, obj: PartnerChannel) -> int:
        return obj.attribution_total

    @admin.display(description="Accruals")
    def accrual_total(self, obj: PartnerChannel) -> int:
        return obj.accrual_total

    @admin.display(description="Grants")
    def grant_total(self, obj: PartnerChannel) -> int:
        return obj.grant_total

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ("id", "created_at", "updated_at")
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organization":
            kwargs["queryset"] = Organization.objects.filter(
                partner_channel__isnull=True,
                account__isnull=False,
            )
        if db_field.name == "owner_user":
            kwargs["queryset"] = User.objects.filter(
                individual_partner_channel__isnull=True,
                billing_account__kind=AccountKind.PERSONAL,
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change) -> None:
        if change:
            update_partner_channel_settings(
                obj.pk,
                is_active=obj.is_active,
                revenue_share_percent=obj.revenue_share_percent,
            )
            return
        try:
            if obj.kind == PartnerChannel.Kind.INDIVIDUAL:
                channel = create_individual_partner_channel(
                    owner=obj.owner_user,
                    revenue_share_percent=obj.revenue_share_percent,
                )
            else:
                channel = create_partner_channel(
                    organization=obj.organization,
                    revenue_share_percent=obj.revenue_share_percent,
                )
        except PartnerInviteError as exc:
            messages.error(request, str(exc))
            return
        if channel.is_active != obj.is_active:
            channel = update_partner_channel_settings(
                channel.pk,
                is_active=obj.is_active,
                revenue_share_percent=channel.revenue_share_percent,
            )
        obj.pk = channel.pk

    @admin.display(description="Settlement account")
    def settlement_account_id(self, obj: PartnerChannel) -> str:
        try:
            account = resolve_partner_settlement_account(obj)
        except (PartnerSettlementAccountMissing, PartnerChannelOwnershipInvalid):
            return "missing"
        return str(account.pk)

    @admin.display(description="Invite active")
    def invite_is_active(self, obj: PartnerChannel) -> str:
        try:
            link = canonical_invite_link(obj)
        except PartnerInviteLink.DoesNotExist:
            return "missing"
        return "yes" if link.is_active else "no"

    @admin.display(description="Customers")
    def attribution_count(self, obj: PartnerChannel) -> int:
        return CustomerAttribution.objects.filter(partner_channel=obj).count()

    @admin.display(description="Earned")
    def earned_sum(self, obj: PartnerChannel) -> str:
        total = PartnerMarginAccrual.objects.filter(partner_channel=obj).aggregate(
            total=Sum("partner_share")
        )["total"]
        return "0.000000" if total is None else f"{total:.6f}"

    @admin.display(description="Grants")
    def grant_count(self, obj: PartnerChannel) -> int:
        return PartnerCreditGrant.objects.filter(partner_channel=obj).count()


@admin.register(PartnerInviteLink)
class PartnerInviteLinkAdmin(admin.ModelAdmin):
    """Token stays readonly. Classification freezes in the form after a visit.

    ``save`` rejects the same fields even if the form is bypassed. Regenerate
    is not this form; it writes ``token`` and ``regenerated_at`` only.
    Add mints a token because this form does not post one.
    """

    list_display = ("partner_channel", "name", "is_active", "created_at")
    readonly_fields = ("id", "token", "created_at", "updated_at", "regenerated_at")

    def get_readonly_fields(self, request, obj=None):
        fields = list(self.readonly_fields)
        if obj is not None and obj.visits.exists():
            fields.extend(["partner_channel", "source", "campaign", "content"])
        return fields

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def save_model(self, request, obj, form, change) -> None:
        if change or obj.token:
            super().save_model(request, obj, form, change)
            return

        def _persist() -> None:
            super(PartnerInviteLinkAdmin, self).save_model(request, obj, form, change)

        persist_with_unique_invite_token(obj, _persist)
