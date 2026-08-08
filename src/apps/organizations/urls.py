"""Organization HTTP routes under ``/api/v1/orgs/``."""

from django.urls import path

from apps.organizations.views import (
    OrganizationDetailView,
    OrganizationInviteAcceptView,
    OrganizationInviteListCreateView,
    OrganizationInviteRevokeView,
    OrganizationListView,
    OrganizationMembersListView,
)

urlpatterns = [
    path("", OrganizationListView.as_view(), name="organization-list"),
    path(
        "invites/accept/",
        OrganizationInviteAcceptView.as_view(),
        name="organization-invite-accept",
    ),
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
    path(
        "<uuid:organization_id>/invites/",
        OrganizationInviteListCreateView.as_view(),
        name="organization-invites",
    ),
    path(
        "<uuid:organization_id>/invites/<uuid:invite_id>/revoke/",
        OrganizationInviteRevokeView.as_view(),
        name="organization-invite-revoke",
    ),
]
