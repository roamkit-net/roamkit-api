"""Internal partner routes. Excluded from the public partner schema."""

from django.urls import path

from apps.billing.partner_internal_views import PartnerConsumeView, PartnerJoinSignView

urlpatterns = [
    path("join-sign/", PartnerJoinSignView.as_view(), name="partner-join-sign"),
    path("consume/", PartnerConsumeView.as_view(), name="partner-consume"),
]
