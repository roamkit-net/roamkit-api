"""My eSIM URL configuration."""

from django.urls import path

from apps.esims.views import (
    EsimDetailView,
    EsimListView,
    EsimTopupsView,
    EsimUsageView,
)

urlpatterns = [
    path("esims/", EsimListView.as_view(), name="me-esim-list"),
    path("esims/<int:pk>/", EsimDetailView.as_view(), name="me-esim-detail"),
    path("esims/<int:pk>/usage/", EsimUsageView.as_view(), name="me-esim-usage"),
    path("esims/<int:pk>/topups/", EsimTopupsView.as_view(), name="me-esim-topups"),
]
