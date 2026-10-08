"""Read-only partner admin plus the one creation service (ADR 023)."""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import Sum

from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerCreditGrant,
    PartnerInviteLink,
    PartnerMarginAccrual,
)
from apps.billing.services.partner_invite import (
    PartnerInviteError,
    create_partner_channel,
)
from apps.organizations.models import Organization


@admin.register(PartnerChannel)
class PartnerChannelAdmin(admin.ModelAdmin):
    list_display = (
        "organization",
        "is_active",
        "revenue_share_percent",
        "team_balance",
        "invite_is_active",
        "attribution_count",
        "earned_sum",
        "grant_count",
    )
    readonly_fields = (
        "id",
        "organization",
        "is_active",
        "revenue_share_percent",
        "created_at",
        "updated_at",
        "team_balance",
        "invite_is_active",
        "attribution_count",
        "earned_sum",
        "grant_count",
    )
    fields = ("organization", "revenue_share_percent")

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return self.readonly_fields

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "organization":
            kwargs["queryset"] = Organization.objects.filter(
                partner_channel__isnull=True,
                account__isnull=False,
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change) -> None:
        if change:
            return
        try:
            channel = create_partner_channel(
                organization=obj.organization,
                revenue_share_percent=obj.revenue_share_percent,
            )
        except PartnerInviteError as exc:
            messages.error(request, str(exc))
            return
        obj.pk = channel.pk

    @admin.display(description="Team balance")
    def team_balance(self, obj: PartnerChannel) -> str:
        account = getattr(obj.organization, "account", None)
        if account is None:
            return "missing"
        return str(account.balance)

    @admin.display(description="Invite active")
    def invite_is_active(self, obj: PartnerChannel) -> str:
        link = PartnerInviteLink.objects.filter(partner_channel=obj).first()
        if link is None:
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
