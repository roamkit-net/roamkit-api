"""Public eSIM routes under ``/api/v1/public/`` (no user JWT)."""

from django.urls import path

from apps.esims.public_views import PublicEsimStatusView

urlpatterns = [
    path("esim/status/", PublicEsimStatusView.as_view(), name="public-esim-status"),
]
