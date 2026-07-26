"""Auth URL configuration."""

from django.urls import path

from apps.accounts.views import (
    ActivateView,
    AuthTokenObtainView,
    AuthTokenRefreshView,
    MeView,
    PasswordResetConfirmView,
    PasswordResetRequestView,
    RegisterView,
)

urlpatterns = [
    path("register/", RegisterView.as_view(), name="auth-register"),
    path("activate/", ActivateView.as_view(), name="auth-activate"),
    path(
        "password-reset/",
        PasswordResetRequestView.as_view(),
        name="auth-password-reset",
    ),
    path(
        "password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="auth-password-reset-confirm",
    ),
    path("token/", AuthTokenObtainView.as_view(), name="auth-token"),
    path("token/refresh/", AuthTokenRefreshView.as_view(), name="auth-token-refresh"),
    path("me/", MeView.as_view(), name="auth-me"),
]
