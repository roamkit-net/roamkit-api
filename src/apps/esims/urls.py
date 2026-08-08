"""My eSIM URL configuration."""

from django.urls import path

from apps.esims.views import (
    EsimArchiveView,
    EsimAutoTopupView,
    EsimDetailView,
    EsimEventsView,
    EsimListView,
    EsimTopupsView,
    EsimUnarchiveView,
    EsimUsageView,
)

urlpatterns = [
    path("esims/", EsimListView.as_view(), name="me-esim-list"),
    path("esims/<int:pk>/", EsimDetailView.as_view(), name="me-esim-detail"),
    path(
        "esims/<int:pk>/archive/",
        EsimArchiveView.as_view(),
        name="me-esim-archive",
    ),
    path(
        "esims/<int:pk>/unarchive/",
        EsimUnarchiveView.as_view(),
        name="me-esim-unarchive",
    ),
    path("esims/<int:pk>/usage/", EsimUsageView.as_view(), name="me-esim-usage"),
    path("esims/<int:pk>/events/", EsimEventsView.as_view(), name="me-esim-events"),
    path("esims/<int:pk>/topups/", EsimTopupsView.as_view(), name="me-esim-topups"),
    path(
        "esims/<int:pk>/auto-topup/",
        EsimAutoTopupView.as_view(),
        name="me-esim-auto-topup",
    ),
]
