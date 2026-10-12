"""Admin registration for esims."""

import logging
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.decorators import method_decorator
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.billing.models import AccountKind
from apps.esims.exceptions import EsimTransferError
from apps.esims.models import (
    Esim,
    EsimAppliedPackage,
    EsimAutoTopupPolicy,
    EsimLifecycleEvent,
    Topup,
)
from apps.esims.services.applied_package_sync import sync_applied_packages
from apps.esims.services.inventory_transfer import transfer_esim_inventory
from apps.esims.services.usage_service import UsageService
from apps.pricing.money import money_round
from shared.providers.factory import get_topup_provider


def _usd(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f"${money_round(value)} USD"


def paid_price_usd(topup: Topup | None) -> str:
    """Customer charge, including a pricing-profile discount."""
    if topup is None:
        return ""
    return _usd(topup.amount)


def wholesale_price_usd(topup: Topup | None) -> str:
    """Wholesale cost. Sequence 0 falls back to the order when the row is blank."""
    if topup is None:
        return ""
    net = topup.net_price_usd
    if net is None and topup.sequence == 0:
        order = topup.esim.order
        net = order.net_price_usd if order is not None else None
    return _usd(net)


_OWNERSHIP_FIELDS = ("account", "user", "assigned_user", "order")
logger = logging.getLogger(__name__)


def personal_destination_users():
    """Users whose billing account is their own personal account."""
    return User.objects.filter(
        billing_account__kind=AccountKind.PERSONAL,
        billing_account__user_id=F("pk"),
    ).order_by("email")


class EsimInventoryTransferForm(forms.Form):
    """Pick the user whose personal account will own the eSIM."""

    destination_user = forms.ModelChoiceField(
        queryset=User.objects.none(),
        label="Destination user",
        help_text="Inventory moves to this user's personal account.",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fields["destination_user"].queryset = personal_destination_users()


class EsimAppliedPackageInline(admin.TabularInline):
    """Read-only Airalo package snapshot. Does not call the provider."""

    model = EsimAppliedPackage
    extra = 0
    can_delete = False
    fields = (
        "sequence",
        "kind",
        "package_external_id",
        "data_allowance",
        "validity_days",
        "status",
        "remaining_mb",
        "amount_mb",
        "activated_at",
        "expired_at",
        "provider_price_usd",
        "paid_price",
        "wholesale_price",
    )
    readonly_fields = fields
    ordering = ("sequence",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("topup", "esim__order")

    def has_add_permission(self, request, obj=None) -> bool:
        return False

    @admin.display(description="Paid")
    def paid_price(self, obj: EsimAppliedPackage) -> str:
        return paid_price_usd(obj.topup)

    @admin.display(description="Wholesale")
    def wholesale_price(self, obj: EsimAppliedPackage) -> str:
        return wholesale_price_usd(obj.topup)


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    change_form_template = "admin/esims/esim/change_form.html"
    inlines = (EsimAppliedPackageInline,)
    list_display = (
        "id",
        "iccid",
        "account",
        "user",
        "assigned_user",
        "order",
        "operator_title",
        "status",
        "activation_policy",
        "note_preview",
        "archived_at",
        "created_at",
    )
    list_filter = ("status", "activation_policy", "operator_title", "archived_at")
    search_fields = (
        "iccid",
        "user__email",
        "assigned_user__email",
        "account__id",
        "matching_id",
        "note",
    )
    readonly_fields = (
        "operator_title",
        "package_balance",
        "created_at",
        "updated_at",
        "usage_synced_at",
        "setup_completed_at",
        "setup_skipped_at",
        "archived_at",
    )
    raw_id_fields = ("account", "user", "assigned_user", "order")

    def get_readonly_fields(self, request, obj=None):
        readonly = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            for name in (*_OWNERSHIP_FIELDS, "transfer_link"):
                if name not in readonly:
                    readonly.append(name)
        return readonly

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("order")
            .prefetch_related("applied_packages")
        )

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if obj is not None and "package_balance" not in fields:
            fields.insert(fields.index("operator_title") + 1, "package_balance")
        if obj is not None and "transfer_link" not in fields:
            fields.append("transfer_link")
        return fields

    @admin.display(description="Data balance")
    def package_balance(self, obj: Esim) -> str:
        total = 0
        left = 0
        finite = False
        for row in obj.applied_packages.all():
            if row.is_unlimited or row.amount_mb is None:
                continue
            finite = True
            total += row.amount_mb
            left += row.remaining_mb or 0
        if not finite or total <= 0:
            return "No synced package snapshot."
        used = max(total - left, 0)
        left_pct = round(100 * left / total)
        used_pct = round(100 * used / total)
        return (
            f"Data left: {left} MB ({left_pct}%). "
            f"Data used: {used} MB ({used_pct}%)."
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<path:object_id>/refresh-usage/",
                self.admin_site.admin_view(self.refresh_usage_view),
                name="esims_esim_refresh_usage",
            ),
            path(
                "<path:object_id>/transfer/",
                self.admin_site.admin_view(self.transfer_view),
                name="esims_esim_transfer",
            ),
        ]
        return custom + urls

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["transfer_url"] = reverse(
            "admin:esims_esim_transfer",
            args=[object_id],
        )
        extra_context["refresh_usage_url"] = reverse(
            "admin:esims_esim_refresh_usage",
            args=[object_id],
        )
        return super().change_view(
            request,
            object_id,
            form_url,
            extra_context=extra_context,
        )

    @admin.display(description="Transfer")
    def transfer_link(self, obj: Esim) -> str:
        url = reverse("admin:esims_esim_transfer", args=[obj.pk])
        return format_html('<a href="{}">Transfer…</a>', url)

    @method_decorator(require_POST)
    def refresh_usage_view(self, request: HttpRequest, object_id) -> HttpResponse:
        """Fetch one live Airalo usage snapshot from an explicit admin POST."""
        if not request.user.has_perm("esims.change_esim"):
            raise PermissionDenied
        esim = get_object_or_404(Esim, pk=object_id)
        provider = get_topup_provider()
        try:
            usage = UsageService(provider).get_usage(esim)
        except Exception:
            logger.exception(
                "Admin Airalo usage refresh failed for esim_id=%s", esim.pk
            )
            messages.error(
                request,
                "Airalo usage refresh failed. Existing usage data was kept.",
            )
            return HttpResponseRedirect(
                reverse("admin:esims_esim_change", args=[esim.pk])
            )
        try:
            package_count = sync_applied_packages(esim, provider)
        except Exception:
            logger.exception(
                "Admin Airalo package snapshot failed for esim_id=%s", esim.pk
            )
            messages.warning(
                request,
                "Airalo usage was refreshed. Package snapshot was kept.",
            )
        else:
            message = (
                f"Refreshed Airalo usage: provider status "
                f"{usage.status or '(empty)'}, local status {esim.status}, "
                f"remaining {usage.remaining_mb}/{usage.total_mb} MB. "
                f"Stored {package_count} package row(s)."
            )
            self.log_change(request, esim, message)
            messages.success(request, message)
        return HttpResponseRedirect(reverse("admin:esims_esim_change", args=[esim.pk]))

    def transfer_view(self, request: HttpRequest, object_id) -> HttpResponse:
        if not request.user.has_perm("esims.change_esim"):
            raise PermissionDenied
        esim = get_object_or_404(
            Esim.objects.select_related("account", "account__user", "user"),
            pk=object_id,
        )
        if request.method == "POST":
            form = EsimInventoryTransferForm(request.POST)
            if form.is_valid():
                destination = form.cleaned_data["destination_user"]
                try:
                    result = transfer_esim_inventory(
                        esim_id=esim.pk,
                        destination_user=destination,
                    )
                except EsimTransferError as exc:
                    messages.error(request, str(exc))
                else:
                    if result.transferred:
                        message = (
                            f"Transferred inventory from account "
                            f"{result.old_account_id} to account "
                            f"{result.new_account_id} for user {destination.pk} "
                            f"({destination.email})."
                        )
                        if result.auto_topup_disabled:
                            message += " Auto top-up disabled."
                        self.log_change(request, esim, message)
                        messages.success(request, message)
                    else:
                        messages.info(
                            request,
                            "This eSIM is already owned by that personal account.",
                        )
                    return HttpResponseRedirect(
                        reverse("admin:esims_esim_change", args=[esim.pk])
                    )
        else:
            form = EsimInventoryTransferForm()
        context = {
            **self.admin_site.each_context(request),
            "title": f"Transfer eSIM {esim.iccid}",
            "esim": esim,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/esims/esim/transfer.html", context)

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
        "sequence",
        "account",
        "esim",
        "package_external_id",
        "amount",
        "status",
        "provider_package_status",
        "external_order_id",
        "created_at",
    )
    list_filter = ("status", "provider_package_status", "sequence")
    search_fields = (
        "id",
        "package_external_id",
        "external_order_id",
        "esim__iccid",
        "account__user__email",
    )
    readonly_fields = (
        "id",
        "sequence",
        "provider_package_status",
        "airalo_plan",
        "airalo_activated_at",
        "airalo_expires_at",
        "airalo_amount",
        "paid_price",
        "wholesale_price",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("account", "esim")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("applied_package", "esim__order")
        )

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        for name in (
            "airalo_plan",
            "airalo_activated_at",
            "airalo_expires_at",
            "airalo_amount",
            "paid_price",
            "wholesale_price",
        ):
            if name not in fields:
                fields.append(name)
        return fields

    @staticmethod
    def _snapshot(obj: Topup) -> EsimAppliedPackage | None:
        return getattr(obj, "applied_package", None)

    @admin.display(description="Plan")
    def airalo_plan(self, obj: Topup) -> str:
        snap = self._snapshot(obj)
        if snap is None:
            return ""
        allowance = snap.data_allowance or ""
        days = f"{snap.validity_days} days" if snap.validity_days else ""
        return " · ".join(part for part in (allowance, days) if part)

    @admin.display(description="Activated on")
    def airalo_activated_at(self, obj: Topup):
        snap = self._snapshot(obj)
        return snap.activated_at if snap is not None else None

    @admin.display(description="Expires on")
    def airalo_expires_at(self, obj: Topup):
        snap = self._snapshot(obj)
        return snap.expired_at if snap is not None else None

    @admin.display(description="Airalo amount")
    def airalo_amount(self, obj: Topup) -> str:
        snap = self._snapshot(obj)
        if snap is None or snap.provider_price_usd is None:
            return ""
        return _usd(snap.provider_price_usd)

    @admin.display(description="Paid")
    def paid_price(self, obj: Topup) -> str:
        return paid_price_usd(obj)

    @admin.display(description="Wholesale")
    def wholesale_price(self, obj: Topup) -> str:
        return wholesale_price_usd(obj)


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
