"""Admin for billing — list/filter plus money ops via CreditService (ADR-010)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html

from apps.billing.exceptions import (
    CreditServiceError,
    DepositVerificationError,
    InsufficientFundsError,
)
from apps.billing.models import (
    Account,
    CreditLedgerEntry,
    DepositRequest,
    LedgerReferenceType,
    Subscription,
)
from apps.billing.services import (
    collect_billing_metrics,
    credit_service,
    deposit_verification_service,
    resolve_reference,
)
from apps.billing.services.metrics import spend_by_reference_type
from shared.events.billing_events import CreditDebited, CreditGranted
from shared.events.event_bus import event_bus


class AdminAdjustForm(forms.Form):
    direction = forms.ChoiceField(
        choices=(("credit", "Credit (add)"), ("debit", "Debit (remove)")),
    )
    amount = forms.DecimalField(
        min_value=Decimal("0.000001"),
        max_digits=20,
        decimal_places=6,
    )
    reason = forms.CharField(
        min_length=3,
        max_length=512,
        widget=forms.Textarea,
    )


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "balance", "version", "created_at", "adjust_link")
    search_fields = ("user__email", "id")
    readonly_fields = (
        "id",
        "balance",
        "version",
        "created_at",
        "updated_at",
    )
    raw_id_fields = ("user",)
    actions = ("rebuild_selected_balances",)

    @admin.display(description="Adjust")
    def adjust_link(self, obj: Account) -> str:
        url = reverse("admin:billing_account_adjust", args=[obj.pk])
        return format_html('<a href="{}">Adjust…</a>', url)

    @admin.action(description="Rebuild selected balances from ledger")
    def rebuild_selected_balances(self, request: HttpRequest, queryset) -> None:
        repaired = 0
        for account in queryset:
            before = account.balance
            after = credit_service.rebuild_balance_from_ledger(account)
            if before != after:
                repaired += 1
        self.message_user(
            request,
            f"Rebuilt {queryset.count()} account(s); {repaired} cache(s) changed.",
            messages.SUCCESS,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "metrics/",
                self.admin_site.admin_view(self.metrics_view),
                name="billing_metrics",
            ),
            path(
                "<uuid:account_id>/adjust/",
                self.admin_site.admin_view(self.adjust_view),
                name="billing_account_adjust",
            ),
        ]
        return custom + urls

    def metrics_view(self, request: HttpRequest) -> HttpResponse:
        if not request.user.is_staff:
            raise PermissionDenied
        metrics = collect_billing_metrics()
        spend = spend_by_reference_type()
        context = {
            **self.admin_site.each_context(request),
            "title": "Billing metrics",
            "metrics": metrics,
            "spend_by_type": spend,
            "opts": self.model._meta,
        }
        return render(request, "admin/billing/metrics.html", context)

    def adjust_view(self, request: HttpRequest, account_id) -> HttpResponse:
        if not request.user.has_perm("billing.change_account"):
            raise PermissionDenied
        account = get_object_or_404(Account, pk=account_id)
        if request.method == "POST":
            form = AdminAdjustForm(request.POST)
            if form.is_valid():
                try:
                    is_credit = form.cleaned_data["direction"] == "credit"
                    amount = form.cleaned_data["amount"]
                    entry = credit_service.admin_adjust(
                        account,
                        amount,
                        credit=is_credit,
                        reason=form.cleaned_data["reason"],
                        actor_id=request.user.pk,
                    )
                except (
                    InsufficientFundsError,
                    CreditServiceError,
                    InvalidOperation,
                ) as exc:
                    messages.error(request, str(exc))
                else:
                    if is_credit:
                        event_bus.publish(
                            CreditGranted(
                                account_id=str(account.pk),
                                amount=amount,
                                balance_after=entry.balance_after,
                                reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
                                reference_id=entry.reference_id,
                                ledger_entry_id=str(entry.pk),
                                created_at=entry.created_at,
                            )
                        )
                    else:
                        event_bus.publish(
                            CreditDebited(
                                account_id=str(account.pk),
                                amount=amount,
                                balance_after=entry.balance_after,
                                reference_type=LedgerReferenceType.ADMIN_ADJUSTMENT,
                                reference_id=entry.reference_id,
                                ledger_entry_id=str(entry.pk),
                                created_at=entry.created_at,
                            )
                        )
                    messages.success(
                        request,
                        f"Adjustment applied: delta={entry.delta}, "
                        f"balance_after={entry.balance_after}. "
                        f"Reason: {form.cleaned_data['reason']}",
                    )
                    return HttpResponseRedirect(
                        reverse("admin:billing_account_change", args=[account.pk])
                    )
        else:
            form = AdminAdjustForm()
        context = {
            **self.admin_site.each_context(request),
            "title": f"Adjust balance — {account}",
            "account": account,
            "form": form,
            "opts": self.model._meta,
        }
        return render(request, "admin/billing/account_adjust.html", context)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["billing_metrics_url"] = reverse("admin:billing_metrics")
        return super().changelist_view(request, extra_context=extra_context)


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
    actions = ("reverify_selected",)

    @admin.action(description="Re-verify selected deposits")
    def reverify_selected(self, request: HttpRequest, queryset) -> None:
        ok = 0
        errors = 0
        for deposit in queryset:
            try:
                deposit_verification_service.reverify(deposit)
                ok += 1
            except (DepositVerificationError, Exception) as exc:
                errors += 1
                self.message_user(
                    request,
                    f"Deposit {deposit.pk}: {exc}",
                    messages.ERROR,
                )
        if ok:
            self.message_user(
                request,
                f"Re-verified {ok} deposit(s).",
                messages.SUCCESS,
            )
        if errors and not ok:
            self.message_user(
                request,
                f"{errors} deposit(s) failed re-verify.",
                messages.WARNING,
            )


@admin.register(CreditLedgerEntry)
class CreditLedgerEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "delta",
        "balance_after",
        "reference_type",
        "reference_id",
        "reference_link",
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
        "reference_link",
        "idempotency_key",
        "created_at",
    )

    @admin.display(description="Reference")
    def reference_link(self, obj: CreditLedgerEntry) -> str:
        related = resolve_reference(obj.reference_type, obj.reference_id)
        if related is None:
            return obj.reference_id
        opts = related._meta
        try:
            url = reverse(
                f"admin:{opts.app_label}_{opts.model_name}_change",
                args=[related.pk],
            )
        except Exception:
            return obj.reference_id
        return format_html(
            '<a href="{}">{} {}</a>',
            url,
            opts.verbose_name,
            related.pk,
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
