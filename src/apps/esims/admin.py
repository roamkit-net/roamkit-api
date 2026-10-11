"""Admin registration for esims."""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils.html import format_html

from apps.accounts.models import User
from apps.billing.models import AccountKind
from apps.esims.exceptions import EsimTransferError
from apps.esims.models import Esim, EsimAutoTopupPolicy, EsimLifecycleEvent, Topup
from apps.esims.services.inventory_transfer import transfer_esim_inventory

_OWNERSHIP_FIELDS = ("account", "user", "assigned_user", "order")


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


@admin.register(Esim)
class EsimAdmin(admin.ModelAdmin):
    change_form_template = "admin/esims/esim/change_form.html"
    list_display = (
        "id",
        "iccid",
        "account",
        "user",
        "assigned_user",
        "order",
        "status",
        "activation_policy",
        "note_preview",
        "archived_at",
        "created_at",
    )
    list_filter = ("status", "activation_policy", "archived_at")
    search_fields = (
        "iccid",
        "user__email",
        "assigned_user__email",
        "account__id",
        "matching_id",
        "note",
    )
    readonly_fields = (
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

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if obj is not None and "transfer_link" not in fields:
            fields.append("transfer_link")
        return fields

    def get_urls(self):
        urls = super().get_urls()
        custom = [
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
        "account",
        "esim",
        "package_external_id",
        "amount",
        "status",
        "external_order_id",
        "created_at",
    )
    list_filter = ("status",)
    search_fields = (
        "id",
        "package_external_id",
        "external_order_id",
        "esim__iccid",
        "account__user__email",
    )
    readonly_fields = ("id", "created_at", "updated_at")
    raw_id_fields = ("account", "esim")


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
