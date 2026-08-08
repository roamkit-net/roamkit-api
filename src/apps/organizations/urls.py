"""Organization HTTP routes under ``/api/v1/orgs/``."""

from django.urls import path

from apps.organizations.views import (
    OrganizationDetailView,
    OrganizationListView,
    OrganizationMembersListView,
)

urlpatterns = [
    path("", OrganizationListView.as_view(), name="organization-list"),
    path(
        "<uuid:organization_id>/",
        OrganizationDetailView.as_view(),
        name="organization-detail",
    ),
    path(
        "<uuid:organization_id>/members/",
        OrganizationMembersListView.as_view(),
        name="organization-members",
    ),
]
