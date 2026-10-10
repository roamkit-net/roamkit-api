"""GET /api/v1/orgs/partner/grants/ (ADR 023)."""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.serializers import (
    PartnerChannelCodeSerializer,
    PartnerGrantsPageSerializer,
    PartnerGrantsQueryError,
    parse_partner_grants_query,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    resolve_partner_summary_channel,
    stamp_partner_role,
)
from apps.billing.services.partner_grants import partner_grants_service
from apps.billing.services.partner_log import PartnerLegacyUsageMixin

_NO_STORE = "no-store"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_grants",
        summary="Partner channel grant history",
        description="Stored credit grants for the caller's single partner channel.",
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, required=False),
            OpenApiParameter("page_size", OpenApiTypes.INT, required=False),
            OpenApiParameter(
                "sort",
                OpenApiTypes.STR,
                required=False,
                enum=["created_at", "amount"],
            ),
            OpenApiParameter(
                "order",
                OpenApiTypes.STR,
                required=False,
                enum=["asc", "desc"],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=PartnerGrantsPageSerializer,
                description="Grant page",
            ),
            400: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description=(
                    "invalid_page, invalid_page_size, invalid_sort, or invalid_order"
                ),
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_access_denied",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled",
            ),
            409: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_context_ambiguous",
            ),
        },
    ),
)
class PartnerGrantsView(PartnerLegacyUsageMixin, APIView):
    legacy_endpoint = "orgs.partner.grants"
    """Read grant history. Auth, flag, summary resolver, then the list service."""

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _coded(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        return super().handle_exception(exc)

    def get(self, request: Request) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            channel = resolve_partner_summary_channel(request.user)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        except PartnerContextAmbiguous:
            return _coded(status.HTTP_409_CONFLICT, "partner_context_ambiguous")
        try:
            query = parse_partner_grants_query(request.query_params)
        except PartnerGrantsQueryError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)

        page = partner_grants_service.list_grants(channel, query)
        response = Response(
            PartnerGrantsPageSerializer(
                {
                    "count": page.count,
                    "page": page.page,
                    "page_size": page.page_size,
                    "results": [
                        {
                            "grant_id": row.grant_id,
                            "customer_id": row.customer_id,
                            "email": row.email,
                            "display_name": row.display_name,
                            "amount": row.amount,
                            "granted_by": (
                                None
                                if row.granted_by is None
                                else {
                                    "user_id": row.granted_by.user_id,
                                    "email": row.granted_by.email,
                                    "display_name": row.granted_by.display_name,
                                }
                            ),
                            "created_at": row.created_at,
                        }
                        for row in page.results
                    ],
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        stamp_partner_role(response, request.user, channel)
        return response
