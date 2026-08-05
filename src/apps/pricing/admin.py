"""Django admin for pricing profiles (ADR 019)."""

from __future__ import annotations

from django.contrib import admin, messages
from django.http import HttpRequest

from apps.pricing.models import PricingProfile, PricingProfileAudit


class PricingProfileAuditInline(admin.TabularInline):
    model = PricingProfileAudit
    extra = 0
    can_delete = False
    readonly_fields = (
        "id",
        "actor",
        "reason",
        "old_values",
        "new_values",
        "created_at",
    )
    ordering = ("-created_at",)

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PricingProfile)
class PricingProfileAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "slug",
        "discount_percent",
        "floor_policy",
        "version",
        "is_active",
        "effective_from",
        "effective_until",
        "archived_at",
    )
    list_filter = ("is_active", "floor_policy", "archived_at")
    search_fields = ("name", "slug")
    readonly_fields = ("id", "version", "archived_at", "created_at", "updated_at")
    prepopulated_fields = {"slug": ("name",)}
    inlines = (PricingProfileAuditInline,)
    actions = ("archive_selected",)

    def delete_model(self, request, obj: PricingProfile) -> None:
        obj.archive()

    def delete_queryset(self, request, queryset) -> None:
        for obj in queryset:
            obj.archive()

    def save_model(self, request, obj: PricingProfile, form, change) -> None:
        old_values: dict = {}
        expected_version: int | None = None
        if change and obj.pk:
            previous = (
                PricingProfile.objects.filter(pk=obj.pk)
                .values(
                    "name",
                    "slug",
                    "discount_percent",
                    "floor_policy",
                    "is_active",
                    "effective_from",
                    "effective_until",
                    "version",
                )
                .first()
            )
            if previous:
                expected_version = previous["version"]
                old_values = {
                    k: str(v) if v is not None else None for k, v in previous.items()
                }
                if not PricingProfile.objects.filter(
                    pk=obj.pk, version=expected_version
                ).exists():
                    self.message_user(
                        request,
                        "Stale version — reload and retry (optimistic lock).",
                        messages.ERROR,
                    )
                    return
                # Align in-memory version with DB before model.save() bump logic.
                obj.version = expected_version

        super().save_model(request, obj, form, change)

        new_values = {
            "name": obj.name,
            "slug": obj.slug,
            "discount_percent": str(obj.discount_percent),
            "floor_policy": obj.floor_policy,
            "is_active": obj.is_active,
            "effective_from": str(obj.effective_from),
            "effective_until": (
                str(obj.effective_until) if obj.effective_until else None
            ),
            "version": obj.version,
        }
        if change and old_values and old_values != new_values:
            PricingProfileAudit.objects.create(
                profile=obj,
                actor=request.user if request.user.is_authenticated else None,
                reason="admin_edit",
                old_values=old_values,
                new_values=new_values,
            )

    @admin.action(description="Archive selected profiles (soft-delete)")
    def archive_selected(self, request: HttpRequest, queryset) -> None:
        count = 0
        for profile in queryset.filter(archived_at__isnull=True):
            old = {"archived_at": None, "is_active": profile.is_active}
            profile.archive()
            PricingProfileAudit.objects.create(
                profile=profile,
                actor=request.user if request.user.is_authenticated else None,
                reason="archive",
                old_values=old,
                new_values={
                    "archived_at": str(profile.archived_at),
                    "is_active": False,
                },
            )
            count += 1
        self.message_user(
            request,
            f"Archived {count} profile(s).",
            messages.SUCCESS,
        )


@admin.register(PricingProfileAudit)
class PricingProfileAuditAdmin(admin.ModelAdmin):
    list_display = ("id", "profile", "actor", "reason", "created_at")
    list_filter = ("reason",)
    search_fields = ("profile__slug", "profile__name", "actor__email")
    readonly_fields = (
        "id",
        "profile",
        "actor",
        "reason",
        "old_values",
        "new_values",
        "created_at",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
