"""Django admin for voucher campaigns, batches, codes, redemptions (ADR 011 PR2)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html

from apps.billing.exceptions import (
    VoucherAdminConflictError,
    VoucherAdminError,
    VoucherAdminValidationError,
    VoucherBatchBusyError,
)
from apps.billing.models import (
    RewardType,
    Voucher,
    VoucherBatch,
    VoucherCampaign,
    VoucherExportAudit,
    VoucherRedemption,
    VoucherRevokeReason,
)
from apps.billing.services import voucher_admin as voucher_admin_svc

PERM_ISSUE = "billing.issue_voucher"
PERM_REVOKE = "billing.revoke_voucher"
PERM_EXPORT = "billing.export_voucher"


def _parse_expected_updated_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _impact_message(impact: voucher_admin_svc.RevokeImpact) -> str:
    return (
        f"Affected vouchers: {impact.affected}. "
        f"Already redeemed: {impact.already_redeemed}. "
        f"Revoked now: {impact.revoked_now}."
    )


class RevokeReasonForm(forms.Form):
    revoke_reason = forms.ChoiceField(choices=VoucherRevokeReason.choices)
    expected_updated_at = forms.CharField(required=False, widget=forms.HiddenInput)


class ExtendExpiryForm(forms.Form):
    expires_at = forms.DateTimeField(
        help_text="New expiry (timezone-aware ISO or admin datetime).",
    )
    expected_updated_at = forms.CharField(required=False, widget=forms.HiddenInput)


class BatchGenerateForm(forms.Form):
    size = forms.IntegerField(min_value=1, max_value=10_000, initial=10)
    credit_amount = forms.DecimalField(
        min_value=Decimal("0.000001"),
        max_digits=20,
        decimal_places=6,
        required=False,
        help_text="Defaults to campaign credit_amount when generating for a campaign.",
    )
    expires_at = forms.DateTimeField(required=False)
    expected_updated_at = forms.CharField(required=False, widget=forms.HiddenInput)
    confirm = forms.BooleanField(
        required=False,
        label="I understand this cannot be undone",
    )


class VoucherSoftDeleteAdminMixin:
    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(VoucherCampaign)
class VoucherCampaignAdmin(VoucherSoftDeleteAdminMixin, admin.ModelAdmin):
    list_display = (
        "code",
        "status",
        "voucher_type",
        "reward_type",
        "credit_amount",
        "expires_at",
        "created_at",
        "generate_batch_link",
    )
    list_filter = ("status", "voucher_type", "reward_type", "redemption_mode")
    search_fields = ("code", "id", "issued_by_id")
    readonly_fields = (
        "id",
        "issued_by_type",
        "issued_by_id",
        "revoked_at",
        "revoked_by_id",
        "created_at",
        "updated_at",
        "admin_warnings_display",
    )
    actions = (
        "activate_selected_campaigns",
        "revoke_selected_campaigns",
        "extend_selected_expiry",
    )
    date_hierarchy = "expires_at"

    def get_queryset(self, request):
        return super().get_queryset(request)

    @admin.display(description="Generate batch")
    def generate_batch_link(self, obj: VoucherCampaign) -> str:
        url = reverse("admin:billing_vouchercampaign_generate_batch", args=[obj.pk])
        return format_html('<a href="{}">Generate…</a>', url)

    @admin.display(description="Warnings")
    def admin_warnings_display(self, obj: VoucherCampaign) -> str:
        warnings = voucher_admin_svc.collect_admin_warnings(campaign=obj)
        if not warnings:
            return "—"
        return format_html("<br>".join(warnings))

    def has_add_permission(self, request) -> bool:
        return request.user.has_perm(PERM_ISSUE)

    def has_change_permission(self, request, obj=None) -> bool:
        if request.user.has_perm(PERM_ISSUE) or request.user.has_perm(PERM_REVOKE):
            return True
        return request.user.has_perm("billing.view_vouchercampaign")

    def save_model(self, request, obj, form, change):
        if not request.user.has_perm(PERM_ISSUE):
            raise PermissionDenied
        if not change:
            obj.issued_by_type = obj.issued_by_type or "admin"
            obj.issued_by_id = str(request.user.pk)
            if obj.reward_type != RewardType.FIXED_CREDIT:
                obj.reward_type = RewardType.FIXED_CREDIT
            super().save_model(request, obj, form, change)
            return
        try:
            voucher_admin_svc.save_campaign(
                obj,
                expected_updated_at=_parse_expected_updated_at(
                    request.POST.get("expected_updated_at")
                    or (
                        obj.updated_at.isoformat()
                        if getattr(obj, "updated_at", None)
                        else None
                    )
                ),
                code=obj.code,
                redemption_mode=obj.redemption_mode,
                voucher_type=obj.voucher_type,
                reward_type=obj.reward_type,
                credit_amount=obj.credit_amount,
                max_redemptions_total=obj.max_redemptions_total,
                max_redemptions_per_account=obj.max_redemptions_per_account,
                starts_at=obj.starts_at,
                expires_at=obj.expires_at,
                status=obj.status,
            )
        except VoucherAdminConflictError as exc:
            messages.error(request, str(exc))
            raise PermissionDenied(str(exc)) from exc
        except VoucherAdminValidationError as exc:
            messages.error(request, str(exc))
            raise PermissionDenied(str(exc)) from exc

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            obj = self.get_object(request, object_id)
            if obj is not None:
                extra_context["voucher_warnings"] = (
                    voucher_admin_svc.collect_admin_warnings(campaign=obj)
                )
                extra_context["expected_updated_at"] = (
                    obj.updated_at.isoformat() if obj.updated_at else ""
                )
        else:
            extra_context["voucher_warnings"] = (
                voucher_admin_svc.collect_admin_warnings()
            )
        return super().changeform_view(
            request, object_id, form_url, extra_context=extra_context
        )

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["voucher_health"] = voucher_admin_svc.campaign_health_stats()
        extra_context["voucher_warnings"] = voucher_admin_svc.collect_admin_warnings()
        return super().changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:campaign_id>/generate-batch/",
                self.admin_site.admin_view(self.generate_batch_view),
                name="billing_vouchercampaign_generate_batch",
            ),
            path(
                "<uuid:campaign_id>/revoke/",
                self.admin_site.admin_view(self.revoke_campaign_view),
                name="billing_vouchercampaign_revoke",
            ),
        ]
        return custom + urls

    def generate_batch_view(self, request: HttpRequest, campaign_id) -> HttpResponse:
        if not request.user.has_perm(PERM_ISSUE):
            raise PermissionDenied
        campaign = get_object_or_404(VoucherCampaign, pk=campaign_id)
        initial = {
            "size": 10,
            "credit_amount": campaign.credit_amount,
            "expires_at": campaign.expires_at,
            "expected_updated_at": (
                campaign.updated_at.isoformat() if campaign.updated_at else ""
            ),
        }
        if request.method == "POST":
            form = BatchGenerateForm(request.POST)
            if form.is_valid():
                size = form.cleaned_data["size"]
                amount = form.cleaned_data["credit_amount"] or campaign.credit_amount
                expires = form.cleaned_data["expires_at"] or campaign.expires_at
                preview = voucher_admin_svc.preview_batch(
                    size=size,
                    credit_amount=amount,
                    reward_type=campaign.reward_type,
                    expires_at=expires,
                )
                if not form.cleaned_data.get("confirm"):
                    context = {
                        **self.admin_site.each_context(request),
                        "title": "Confirm batch generation",
                        "campaign": campaign,
                        "form": form,
                        "preview": preview,
                        "opts": self.model._meta,
                        "voucher_warnings": voucher_admin_svc.collect_admin_warnings(
                            campaign=campaign
                        ),
                    }
                    return render(
                        request,
                        "admin/billing/voucher_batch_preview.html",
                        context,
                    )
                try:
                    batch = voucher_admin_svc.generate_batch(
                        size=size,
                        credit_amount=amount,
                        actor_id=request.user.pk,
                        campaign=campaign,
                        expected_campaign_updated_at=_parse_expected_updated_at(
                            form.cleaned_data.get("expected_updated_at")
                        ),
                        expires_at=expires,
                    )
                except (
                    VoucherAdminConflictError,
                    VoucherBatchBusyError,
                    VoucherAdminValidationError,
                ) as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request,
                        f"Batch {batch.pk} generated ({batch.size} vouchers).",
                    )
                    return HttpResponseRedirect(
                        reverse("admin:billing_voucherbatch_change", args=[batch.pk])
                    )
        else:
            form = BatchGenerateForm(initial=initial)
        preview = voucher_admin_svc.preview_batch(
            size=form.initial.get("size", 10),
            credit_amount=form.initial.get("credit_amount") or campaign.credit_amount,
            reward_type=campaign.reward_type,
            expires_at=form.initial.get("expires_at"),
        )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Generate batch — {campaign}",
            "campaign": campaign,
            "form": form,
            "preview": preview,
            "opts": self.model._meta,
            "voucher_warnings": voucher_admin_svc.collect_admin_warnings(
                campaign=campaign
            ),
        }
        return render(request, "admin/billing/voucher_batch_preview.html", context)

    def revoke_campaign_view(self, request: HttpRequest, campaign_id) -> HttpResponse:
        if not request.user.has_perm(PERM_REVOKE):
            raise PermissionDenied
        campaign = get_object_or_404(VoucherCampaign, pk=campaign_id)
        impact = voucher_admin_svc.preview_revoke_campaign(campaign)
        if request.method == "POST":
            form = RevokeReasonForm(request.POST)
            if form.is_valid():
                try:
                    result = voucher_admin_svc.revoke_campaign(
                        campaign,
                        reason=form.cleaned_data["revoke_reason"],
                        actor_id=request.user.pk,
                        expected_updated_at=_parse_expected_updated_at(
                            form.cleaned_data.get("expected_updated_at")
                        ),
                    )
                except VoucherAdminError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request,
                        f"Campaign revoked. {_impact_message(result)}",
                    )
                    return HttpResponseRedirect(
                        reverse(
                            "admin:billing_vouchercampaign_change",
                            args=[campaign.pk],
                        )
                    )
        else:
            form = RevokeReasonForm(
                initial={
                    "expected_updated_at": (
                        campaign.updated_at.isoformat() if campaign.updated_at else ""
                    )
                }
            )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Revoke campaign — {campaign}",
            "campaign": campaign,
            "form": form,
            "impact": impact,
            "opts": self.model._meta,
        }
        return render(request, "admin/billing/voucher_revoke_confirm.html", context)

    @admin.action(description="Activate selected campaigns")
    def activate_selected_campaigns(self, request: HttpRequest, queryset) -> None:
        if not request.user.has_perm(PERM_ISSUE):
            raise PermissionDenied
        count = voucher_admin_svc.activate_campaigns(queryset)
        self.message_user(
            request,
            f"Activated {count} campaign(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Revoke selected campaigns…")
    def revoke_selected_campaigns(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_REVOKE):
            raise PermissionDenied
        if "apply" in request.POST:
            form = RevokeReasonForm(request.POST)
            if form.is_valid():
                total = voucher_admin_svc.RevokeImpact(0, 0, 0)
                for campaign in queryset:
                    try:
                        impact = voucher_admin_svc.revoke_campaign(
                            campaign,
                            reason=form.cleaned_data["revoke_reason"],
                            actor_id=request.user.pk,
                            expected_updated_at=campaign.updated_at,
                        )
                        total = voucher_admin_svc.RevokeImpact(
                            affected=total.affected + impact.affected,
                            already_redeemed=total.already_redeemed
                            + impact.already_redeemed,
                            revoked_now=total.revoked_now + impact.revoked_now,
                        )
                    except VoucherAdminError as exc:
                        self.message_user(request, str(exc), messages.ERROR)
                self.message_user(
                    request,
                    f"Campaigns revoked. {_impact_message(total)}",
                    messages.SUCCESS,
                )
                return None
        else:
            form = RevokeReasonForm()
        impacts = [(c, voucher_admin_svc.preview_revoke_campaign(c)) for c in queryset]
        context = {
            **self.admin_site.each_context(request),
            "title": "Revoke campaigns",
            "form": form,
            "impacts": impacts,
            "queryset": queryset,
            "opts": self.model._meta,
            "action": "revoke_selected_campaigns",
        }
        return render(request, "admin/billing/voucher_revoke_selected.html", context)

    @admin.action(description="Extend expiry on selected…")
    def extend_selected_expiry(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_ISSUE):
            raise PermissionDenied
        if "apply" in request.POST:
            form = ExtendExpiryForm(request.POST)
            if form.is_valid():
                ok = 0
                for campaign in queryset:
                    try:
                        voucher_admin_svc.extend_campaign_expiry(
                            campaign,
                            expires_at=form.cleaned_data["expires_at"],
                            expected_updated_at=campaign.updated_at,
                        )
                        ok += 1
                    except VoucherAdminError as exc:
                        self.message_user(request, str(exc), messages.ERROR)
                self.message_user(
                    request, f"Extended expiry on {ok} campaign(s).", messages.SUCCESS
                )
                return None
        else:
            form = ExtendExpiryForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Extend campaign expiry",
            "form": form,
            "queryset": queryset,
            "opts": self.model._meta,
            "action": "extend_selected_expiry",
        }
        return render(request, "admin/billing/voucher_extend_selected.html", context)


@admin.register(VoucherBatch)
class VoucherBatchAdmin(VoucherSoftDeleteAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "campaign",
        "size",
        "status",
        "generated_at",
        "generation_duration_ms",
        "generated_by_version",
        "created_at",
        "export_links",
    )
    list_filter = ("status",)
    search_fields = ("id", "campaign__code", "issued_by_id")
    readonly_fields = (
        "id",
        "size",
        "issued_by_type",
        "issued_by_id",
        "generated_at",
        "generation_duration_ms",
        "generated_by_version",
        "created_at",
        "updated_at",
        "admin_warnings_display",
    )
    autocomplete_fields = ("campaign",)
    actions = ("revoke_selected_batches",)

    def has_add_permission(self, request) -> bool:
        return False

    @admin.display(description="Warnings")
    def admin_warnings_display(self, obj: VoucherBatch) -> str:
        warnings = voucher_admin_svc.collect_admin_warnings(batch=obj)
        return format_html("<br>".join(warnings)) if warnings else "—"

    @admin.display(description="Export")
    def export_links(self, obj: VoucherBatch) -> str:
        csv_url = reverse("admin:billing_voucherbatch_export_csv", args=[obj.pk])
        pdf_url = reverse("admin:billing_voucherbatch_export_pdf", args=[obj.pk])
        prev_csv = reverse("admin:billing_voucherbatch_preview_csv", args=[obj.pk])
        prev_pdf = reverse("admin:billing_voucherbatch_preview_pdf", args=[obj.pk])
        return format_html(
            '<a href="{}">CSV</a> | <a href="{}">PDF</a> | '
            '<a href="{}">Preview CSV</a> | <a href="{}">Preview PDF</a>',
            csv_url,
            pdf_url,
            prev_csv,
            prev_pdf,
        )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:batch_id>/export.csv",
                self.admin_site.admin_view(self.export_csv_view),
                name="billing_voucherbatch_export_csv",
            ),
            path(
                "<uuid:batch_id>/export.pdf",
                self.admin_site.admin_view(self.export_pdf_view),
                name="billing_voucherbatch_export_pdf",
            ),
            path(
                "<uuid:batch_id>/preview.csv",
                self.admin_site.admin_view(self.preview_csv_view),
                name="billing_voucherbatch_preview_csv",
            ),
            path(
                "<uuid:batch_id>/preview.pdf",
                self.admin_site.admin_view(self.preview_pdf_view),
                name="billing_voucherbatch_preview_pdf",
            ),
            path(
                "<uuid:batch_id>/revoke/",
                self.admin_site.admin_view(self.revoke_batch_view),
                name="billing_voucherbatch_revoke",
            ),
        ]
        return custom + urls

    def _export_response(
        self,
        request: HttpRequest,
        batch_id,
        *,
        fmt: str,
        preview: bool,
    ) -> HttpResponse:
        if not request.user.has_perm(PERM_EXPORT):
            raise PermissionDenied
        batch = get_object_or_404(VoucherBatch, pk=batch_id)
        if fmt == "csv":
            body = voucher_admin_svc.export_batch_csv(
                batch, actor_id=request.user.pk, record_audit=not preview
            )
            response = HttpResponse(body, content_type="text/csv")
            filename = f"batch-{batch.pk}{'-preview' if preview else ''}.csv"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response
        payload = voucher_admin_svc.export_batch_pdf(
            batch, actor_id=request.user.pk, record_audit=not preview
        )
        response = HttpResponse(payload, content_type="application/pdf")
        filename = f"batch-{batch.pk}{'-preview' if preview else ''}.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    def export_csv_view(self, request, batch_id):
        return self._export_response(request, batch_id, fmt="csv", preview=False)

    def export_pdf_view(self, request, batch_id):
        return self._export_response(request, batch_id, fmt="pdf", preview=False)

    def preview_csv_view(self, request, batch_id):
        return self._export_response(request, batch_id, fmt="csv", preview=True)

    def preview_pdf_view(self, request, batch_id):
        return self._export_response(request, batch_id, fmt="pdf", preview=True)

    def revoke_batch_view(self, request: HttpRequest, batch_id) -> HttpResponse:
        if not request.user.has_perm(PERM_REVOKE):
            raise PermissionDenied
        batch = get_object_or_404(VoucherBatch, pk=batch_id)
        impact = voucher_admin_svc.preview_revoke_batch(batch)
        if request.method == "POST":
            form = RevokeReasonForm(request.POST)
            if form.is_valid():
                try:
                    result = voucher_admin_svc.revoke_batch(
                        batch,
                        reason=form.cleaned_data["revoke_reason"],
                        actor_id=request.user.pk,
                        expected_updated_at=_parse_expected_updated_at(
                            form.cleaned_data.get("expected_updated_at")
                        ),
                    )
                except VoucherAdminError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(
                        request, f"Batch revoked. {_impact_message(result)}"
                    )
                    return HttpResponseRedirect(
                        reverse("admin:billing_voucherbatch_change", args=[batch.pk])
                    )
        else:
            form = RevokeReasonForm(
                initial={
                    "expected_updated_at": (
                        batch.updated_at.isoformat() if batch.updated_at else ""
                    )
                }
            )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Revoke batch — {batch}",
            "batch": batch,
            "form": form,
            "impact": impact,
            "opts": self.model._meta,
        }
        return render(request, "admin/billing/voucher_revoke_confirm.html", context)

    @admin.action(description="Revoke selected batches…")
    def revoke_selected_batches(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_REVOKE):
            raise PermissionDenied
        if "apply" in request.POST:
            form = RevokeReasonForm(request.POST)
            if form.is_valid():
                total = voucher_admin_svc.RevokeImpact(0, 0, 0)
                for batch in queryset:
                    try:
                        impact = voucher_admin_svc.revoke_batch(
                            batch,
                            reason=form.cleaned_data["revoke_reason"],
                            actor_id=request.user.pk,
                            expected_updated_at=batch.updated_at,
                        )
                        total = voucher_admin_svc.RevokeImpact(
                            affected=total.affected + impact.affected,
                            already_redeemed=total.already_redeemed
                            + impact.already_redeemed,
                            revoked_now=total.revoked_now + impact.revoked_now,
                        )
                    except VoucherAdminError as exc:
                        self.message_user(request, str(exc), messages.ERROR)
                self.message_user(
                    request,
                    f"Batches revoked. {_impact_message(total)}",
                    messages.SUCCESS,
                )
                return None
        else:
            form = RevokeReasonForm()
        impacts = [(b, voucher_admin_svc.preview_revoke_batch(b)) for b in queryset]
        context = {
            **self.admin_site.each_context(request),
            "title": "Revoke batches",
            "form": form,
            "impacts": impacts,
            "queryset": queryset,
            "opts": self.model._meta,
            "action": "revoke_selected_batches",
        }
        return render(request, "admin/billing/voucher_revoke_selected.html", context)


class RedeemedFilter(admin.SimpleListFilter):
    title = "redeemed"
    parameter_name = "redeemed"

    def lookups(self, request, model_admin):
        return (("yes", "Redeemed"), ("no", "Not redeemed"))

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(status=Voucher.Status.REDEEMED)
        if self.value() == "no":
            return queryset.exclude(status=Voucher.Status.REDEEMED)
        return queryset


@admin.register(Voucher)
class VoucherAdmin(VoucherSoftDeleteAdminMixin, admin.ModelAdmin):
    list_display = (
        "code",
        "status",
        "credit_amount",
        "campaign",
        "batch",
        "expires_at",
        "redeemed_at_display",
        "redeemed_by_display",
        "revoked_at",
        "revoked_by_id",
        "created_at",
    )
    list_filter = ("status", "reward_type", "voucher_type", RedeemedFilter, "batch")
    search_fields = (
        "code",
        "campaign__code",
        "batch__id",
        "redemptions__account__user__email",
        "id",
    )
    readonly_fields = (
        "id",
        "code",
        "redemption_mode",
        "voucher_type",
        "reward_type",
        "credit_amount",
        "campaign",
        "batch",
        "status",
        "revoke_reason",
        "issued_by_type",
        "issued_by_id",
        "revoked_at",
        "revoked_by_id",
        "created_at",
        "updated_at",
        "redeemed_at_display",
        "redeemed_by_display",
    )
    autocomplete_fields = ("campaign", "batch")
    list_select_related = ("campaign", "batch")
    actions = (
        "revoke_selected_vouchers",
        "extend_selected_voucher_expiry",
        "export_selected_csv",
    )

    def get_queryset(self, request):
        return (
            super().get_queryset(request).prefetch_related("redemptions__account__user")
        )

    def has_add_permission(self, request) -> bool:
        return request.user.has_perm(PERM_ISSUE)

    def has_change_permission(self, request, obj=None) -> bool:
        if obj is not None and obj.status == Voucher.Status.REDEEMED:
            return request.user.has_perm("billing.view_voucher")
        return request.user.has_perm(PERM_ISSUE) or request.user.has_perm(
            "billing.view_voucher"
        )

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.status == Voucher.Status.REDEEMED:
            return fields + ["expires_at"]
        if "expires_at" in fields:
            fields.remove("expires_at")
        return fields

    def _first_redemption(self, obj: Voucher):
        redemptions = list(obj.redemptions.all()[:1])
        return redemptions[0] if redemptions else None

    @admin.display(description="Redeemed at")
    def redeemed_at_display(self, obj: Voucher) -> str:
        redemption = self._first_redemption(obj)
        return str(redemption.redeemed_at) if redemption else "—"

    @admin.display(description="Redeemed by")
    def redeemed_by_display(self, obj: Voucher) -> str:
        redemption = self._first_redemption(obj)
        if redemption is None:
            return "—"
        user = getattr(redemption.account, "user", None)
        return getattr(user, "email", None) or str(redemption.account_id)

    @admin.action(description="Revoke selected vouchers…")
    def revoke_selected_vouchers(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_REVOKE):
            raise PermissionDenied
        if "apply" in request.POST:
            form = RevokeReasonForm(request.POST)
            if form.is_valid():
                try:
                    impact = voucher_admin_svc.revoke_vouchers(
                        list(queryset),
                        reason=form.cleaned_data["revoke_reason"],
                        actor_id=request.user.pk,
                    )
                except VoucherAdminError as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    self.message_user(
                        request,
                        f"Vouchers revoked. {_impact_message(impact)}",
                        messages.SUCCESS,
                    )
                return None
        else:
            form = RevokeReasonForm()
        impact = voucher_admin_svc.preview_revoke_vouchers(queryset)
        context = {
            **self.admin_site.each_context(request),
            "title": "Revoke vouchers",
            "form": form,
            "impact": impact,
            "queryset": queryset,
            "opts": self.model._meta,
            "action": "revoke_selected_vouchers",
        }
        return render(request, "admin/billing/voucher_revoke_selected.html", context)

    @admin.action(description="Extend expiry on selected…")
    def extend_selected_voucher_expiry(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_ISSUE):
            raise PermissionDenied
        if "apply" in request.POST:
            form = ExtendExpiryForm(request.POST)
            if form.is_valid():
                ok = 0
                for voucher in queryset:
                    try:
                        voucher_admin_svc.extend_voucher_expiry(
                            voucher,
                            expires_at=form.cleaned_data["expires_at"],
                            expected_updated_at=voucher.updated_at,
                        )
                        ok += 1
                    except VoucherAdminError as exc:
                        self.message_user(request, str(exc), messages.ERROR)
                self.message_user(
                    request, f"Extended expiry on {ok} voucher(s).", messages.SUCCESS
                )
                return None
        else:
            form = ExtendExpiryForm()
        context = {
            **self.admin_site.each_context(request),
            "title": "Extend voucher expiry",
            "form": form,
            "queryset": queryset,
            "opts": self.model._meta,
            "action": "extend_selected_voucher_expiry",
        }
        return render(request, "admin/billing/voucher_extend_selected.html", context)

    @admin.action(description="Export selected CSV")
    def export_selected_csv(self, request: HttpRequest, queryset):
        if not request.user.has_perm(PERM_EXPORT):
            raise PermissionDenied
        body = voucher_admin_svc.build_vouchers_csv_text(queryset)
        response = HttpResponse(body, content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="vouchers-selected.csv"'
        return response


@admin.register(VoucherRedemption)
class VoucherRedemptionAdmin(VoucherSoftDeleteAdminMixin, admin.ModelAdmin):
    list_display = (
        "id",
        "account",
        "voucher",
        "campaign",
        "amount",
        "request_id",
        "redeemed_at",
    )
    list_filter = ("campaign",)
    search_fields = (
        "request_id",
        "account__user__email",
        "voucher__code",
        "campaign__code",
        "id",
    )
    readonly_fields = (
        "id",
        "account",
        "voucher",
        "campaign",
        "amount",
        "ledger_entry_id",
        "request_id",
        "redeemed_at",
        "redeemed_ip",
        "redeemed_user_agent",
        "created_at",
    )
    list_select_related = ("account__user", "voucher", "campaign")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(VoucherExportAudit)
class VoucherExportAuditAdmin(VoucherSoftDeleteAdminMixin, admin.ModelAdmin):
    list_display = ("id", "batch", "format", "exported_by_id", "exported_at")
    list_filter = ("format",)
    search_fields = ("batch__id", "exported_by_id", "id")
    readonly_fields = (
        "id",
        "batch",
        "format",
        "exported_by_id",
        "exported_at",
        "created_at",
    )
    list_select_related = ("batch",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
