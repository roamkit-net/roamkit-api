"""Organization HTTP routes under ``/api/v1/orgs/``."""

from django.urls import path

from apps.organizations.views import (
    OrganizationDetailView,
    OrganizationDeviceBindingDetailView,
    OrganizationDeviceBindingListCreateView,
    OrganizationDeviceBindingUnbindView,
    OrganizationDeviceStatusView,
    OrganizationInviteAcceptView,
    OrganizationInviteListCreateView,
    OrganizationInviteRevokeView,
    OrganizationListView,
    OrganizationMemberDetailView,
    OrganizationMemberRevokeView,
    OrganizationMembersListView,
    OrganizationTransferOwnershipView,
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
        "<uuid:organization_id>/transfer-ownership/",
        OrganizationTransferOwnershipView.as_view(),
        name="organization-transfer-ownership",
    ),
    path(
        "<uuid:organization_id>/members/",
        OrganizationMembersListView.as_view(),
        name="organization-members",
    ),
    path(
        "<uuid:organization_id>/members/<uuid:membership_id>/",
        OrganizationMemberDetailView.as_view(),
        name="organization-member-detail",
    ),
    path(
        "<uuid:organization_id>/members/<uuid:membership_id>/revoke/",
        OrganizationMemberRevokeView.as_view(),
        name="organization-member-revoke",
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
    path(
        "<uuid:organization_id>/device-bindings/",
        OrganizationDeviceBindingListCreateView.as_view(),
        name="organization-device-bindings",
    ),
    path(
        "<uuid:organization_id>/device-bindings/<uuid:binding_id>/",
        OrganizationDeviceBindingDetailView.as_view(),
        name="organization-device-binding-detail",
    ),
    path(
        "<uuid:organization_id>/device-bindings/<uuid:binding_id>/unbind/",
        OrganizationDeviceBindingUnbindView.as_view(),
        name="organization-device-binding-unbind",
    ),
    path(
        "<uuid:organization_id>/devices/<str:device_external_id>/status/",
        OrganizationDeviceStatusView.as_view(),
        name="organization-device-status",
    ),
]
