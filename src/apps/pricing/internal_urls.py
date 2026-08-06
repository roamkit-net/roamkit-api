"""Internal pricing URL routes — mounted at ``/api/internal/``."""

from django.urls import path

from apps.pricing.internal_views import PricingPreviewView

urlpatterns = [
    path(
        "pricing/preview",
        PricingPreviewView.as_view(),
        name="internal-pricing-preview",
    ),
]
