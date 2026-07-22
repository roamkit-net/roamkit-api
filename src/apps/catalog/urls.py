"""Catalog URL configuration."""

from django.urls import path

from apps.catalog.views import LocationDetailView, LocationListView, PackageListView

urlpatterns = [
    path("packages/", PackageListView.as_view(), name="package-list"),
    path("locations/", LocationListView.as_view(), name="location-list"),
    path(
        "locations/<slug:slug>/",
        LocationDetailView.as_view(),
        name="location-detail",
    ),
]
