"""Partner portal routes under ``/api/v1/partner/`` (ADR 024)."""

from django.urls import path

from apps.billing.partner_portal_views import (
    PartnerChannelCustomersView,
    PartnerChannelGrantView,
    PartnerChannelInviteActivateView,
    PartnerChannelInviteDeactivateView,
    PartnerChannelInviteLinkView,
    PartnerChannelInviteRegenerateView,
    PartnerChannelSummaryView,
    PartnerContextListView,
)

urlpatterns = [
    path(
        "contexts/",
        PartnerContextListView.as_view(),
        name="partner-contexts",
    ),
    path(
        "channels/<uuid:channel_id>/summary/",
        PartnerChannelSummaryView.as_view(),
        name="partner-channel-summary",
    ),
    path(
        "channels/<uuid:channel_id>/customers/",
        PartnerChannelCustomersView.as_view(),
        name="partner-channel-customers",
    ),
    path(
        "channels/<uuid:channel_id>/grants/",
        PartnerChannelGrantView.as_view(),
        name="partner-channel-grant",
    ),
    path(
        "channels/<uuid:channel_id>/invite-link/",
        PartnerChannelInviteLinkView.as_view(),
        name="partner-channel-invite-link",
    ),
    path(
        "channels/<uuid:channel_id>/invite-link/regenerate/",
        PartnerChannelInviteRegenerateView.as_view(),
        name="partner-channel-invite-link-regenerate",
    ),
    path(
        "channels/<uuid:channel_id>/invite-link/activate/",
        PartnerChannelInviteActivateView.as_view(),
        name="partner-channel-invite-link-activate",
    ),
    path(
        "channels/<uuid:channel_id>/invite-link/deactivate/",
        PartnerChannelInviteDeactivateView.as_view(),
        name="partner-channel-invite-link-deactivate",
    ),
]
