"""Partner invite link HTTP API (ADR 023)."""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.serializers import (
    PartnerChannelCodeSerializer,
    PartnerInviteLinkSerializer,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    resolve_partner_summary_channel,
    stamp_partner_role,
)
from apps.billing.services.partner_invite import (
    PartnerInviteForbidden,
    invite_link_for,
    regenerate_invite_link,
    require_partner_owner,
    set_invite_active,
)
from core.http.request_id import get_or_create_request_id

_NO_STORE = "no-store"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


def _link_response(link) -> dict:
    return {
        "url": link.url,
        "is_active": link.is_active,
        "created_at": link.created_at,
        "regenerated_at": link.regenerated_at,
    }


class _InviteBase(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _coded(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        return super().handle_exception(exc)

    def _channel(self, request: Request):
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            return resolve_partner_summary_channel(request.user)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        except PartnerContextAmbiguous:
            return _coded(status.HTTP_409_CONFLICT, "partner_context_ambiguous")

    def _owner_channel(self, request: Request):
        channel = self._channel(request)
        if isinstance(channel, Response):
            return channel
        try:
            require_partner_owner(request.user, channel)
        except PartnerInviteForbidden:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_invite_forbidden")
        return channel

    def _ok(self, request: Request, link, channel) -> Response:
        response = Response(
            PartnerInviteLinkSerializer(_link_response(link)).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        return response


_INVITE_RESPONSES = {
    200: OpenApiResponse(
        response=PartnerInviteLinkSerializer,
        description="Invite link",
    ),
    401: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="authentication_required",
    ),
    403: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_access_denied or partner_invite_forbidden",
    ),
    404: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_channel_disabled",
    ),
    409: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_context_ambiguous",
    ),
}


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_invite_link",
        summary="Partner invite link",
        responses=_INVITE_RESPONSES,
    ),
)
class PartnerInviteLinkView(_InviteBase):
    def get(self, request: Request) -> Response:
        channel = self._channel(request)
        if isinstance(channel, Response):
            return channel
        response = self._ok(request, invite_link_for(channel), channel)
        stamp_partner_role(response, request.user, channel)
        return response


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_invite_link_regenerate",
        summary="Regenerate partner invite link",
        request=None,
        responses=_INVITE_RESPONSES,
    ),
)
class PartnerInviteRegenerateView(_InviteBase):
    def post(self, request: Request) -> Response:
        channel = self._owner_channel(request)
        if isinstance(channel, Response):
            return channel
        link = regenerate_invite_link(
            channel,
            actor=request.user,
            request_id=get_or_create_request_id(request),
        )
        return self._ok(request, link, channel)


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_invite_link_activate",
        summary="Activate partner invite link",
        request=None,
        responses=_INVITE_RESPONSES,
    ),
)
class PartnerInviteActivateView(_InviteBase):
    def post(self, request: Request) -> Response:
        channel = self._owner_channel(request)
        if isinstance(channel, Response):
            return channel
        link = set_invite_active(
            channel,
            actor=request.user,
            active=True,
            request_id=get_or_create_request_id(request),
        )
        return self._ok(request, link, channel)


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_invite_link_deactivate",
        summary="Deactivate partner invite link",
        request=None,
        responses=_INVITE_RESPONSES,
    ),
)
class PartnerInviteDeactivateView(_InviteBase):
    def post(self, request: Request) -> Response:
        channel = self._owner_channel(request)
        if isinstance(channel, Response):
            return channel
        link = set_invite_active(
            channel,
            actor=request.user,
            active=False,
            request_id=get_or_create_request_id(request),
        )
        return self._ok(request, link, channel)
