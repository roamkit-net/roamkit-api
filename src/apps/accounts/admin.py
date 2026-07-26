"""Admin registration for accounts."""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("email",)
    list_display = (
        "email",
        "wallet_address",
        "last_login_provider",
        "is_staff",
        "is_active",
        "created_at",
    )
    search_fields = ("email", "wallet_address", "google_sub")
    list_filter = ("is_staff", "is_active", "last_login_provider")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Wallet", {"fields": ("wallet_address",)}),
        (
            "Google",
            {
                "fields": (
                    "google_sub",
                    "google_name",
                    "google_picture",
                    "last_login_provider",
                    "last_google_login_at",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2", "is_staff", "is_active"),
            },
        ),
    )
    readonly_fields = (
        "google_sub",
        "last_google_login_at",
        "created_at",
        "updated_at",
    )
    filter_horizontal = ("groups", "user_permissions")
