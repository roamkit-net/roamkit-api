"""URL configuration for roamkit-api."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)
from rest_framework.permissions import AllowAny

from core.health.version import version

schema_view = SpectacularAPIView.as_view(permission_classes=[AllowAny])
swagger_view = SpectacularSwaggerView.as_view(
    url_name="schema",
    permission_classes=[AllowAny],
)
redoc_view = SpectacularRedocView.as_view(
    url_name="schema",
    permission_classes=[AllowAny],
)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("core.health.urls")),
    path("version", version, name="version"),
    path("api/schema/", schema_view, name="schema"),
    path("api/docs/", swagger_view, name="swagger-ui"),
    path("api/redoc/", redoc_view, name="redoc"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/billing/", include("apps.billing.urls")),
    path("api/v1/orders/", include("apps.orders.urls")),
    path("api/v1/orgs/", include("apps.organizations.urls")),
    path("api/v1/device/", include("apps.organizations.device_urls")),
    path("api/v1/me/", include("apps.esims.urls")),
    path("api/v1/admin/", include("apps.ops.urls")),
    path("api/v1/", include("apps.catalog.urls")),
    path("api/internal/", include("apps.pricing.internal_urls")),
]
