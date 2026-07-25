"""URL configuration for roamkit-api."""

from django.contrib import admin
from django.urls import include, path

from core.health.version import version

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("core.health.urls")),
    path("version", version, name="version"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/orders/", include("apps.orders.urls")),
    path("api/v1/me/", include("apps.esims.urls")),
    path("api/v1/", include("apps.catalog.urls")),
]
