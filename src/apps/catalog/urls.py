"""Catalog URL configuration."""

from django.urls import path

from apps.catalog.views import PackageListView

urlpatterns = [
    path("packages/", PackageListView.as_view(), name="package-list"),
]
