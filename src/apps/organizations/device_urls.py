"""Device-facing routes under ``/api/v1/device/`` (no user JWT)."""

from django.urls import path

from apps.organizations.views import (
    DeviceCoverageView,
    DevicePackagesView,
    DeviceStatusView,
)

urlpatterns = [
    path("status/", DeviceStatusView.as_view(), name="device-status"),
    path("coverage/", DeviceCoverageView.as_view(), name="device-coverage"),
    path("packages/", DevicePackagesView.as_view(), name="device-packages"),
]
