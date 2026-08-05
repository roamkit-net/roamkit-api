"""URL routes for ``/api/v1/admin/``."""

from django.urls import path

from apps.ops.views import (
    OpsDashboardView,
    OpsDepositListView,
    OpsHealthView,
    OpsOrderListView,
    OpsSearchView,
    OpsUserDetailView,
    OpsUserListView,
)

urlpatterns = [
    path("dashboard/", OpsDashboardView.as_view(), name="ops-dashboard"),
    path("health/", OpsHealthView.as_view(), name="ops-health"),
    path("search/", OpsSearchView.as_view(), name="ops-search"),
    path("users/", OpsUserListView.as_view(), name="ops-users-list"),
    path("users/<int:pk>/", OpsUserDetailView.as_view(), name="ops-users-detail"),
    path("orders/", OpsOrderListView.as_view(), name="ops-orders-list"),
    path("deposits/", OpsDepositListView.as_view(), name="ops-deposits-list"),
]
