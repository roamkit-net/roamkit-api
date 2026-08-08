"""Device-facing routes under ``/api/v1/device/`` (no user JWT)."""

from django.urls import path

from apps.organizations.views import DeviceStatusView

urlpatterns = [
    path("status/", DeviceStatusView.as_view(), name="device-status"),
]
