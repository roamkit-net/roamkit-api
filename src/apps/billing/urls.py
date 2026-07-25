"""Billing URL configuration — mounted at ``/api/v1/billing/``."""

from django.urls import path

from apps.billing.views import (
    BalanceView,
    BillingConfigView,
    DepositInfoView,
    VerifyCexDepositView,
    VerifyWalletDepositView,
)

urlpatterns = [
    path("config/", BillingConfigView.as_view(), name="billing-config"),
    path("balance/", BalanceView.as_view(), name="billing-balance"),
    path("deposit-info/", DepositInfoView.as_view(), name="billing-deposit-info"),
    path(
        "verify-wallet/",
        VerifyWalletDepositView.as_view(),
        name="billing-verify-wallet",
    ),
    path(
        "verify-cex/",
        VerifyCexDepositView.as_view(),
        name="billing-verify-cex",
    ),
]
