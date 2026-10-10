"""GET /api/v1/orgs/partner/customers/ (ADR 023)."""

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
    PartnerCustomersPageSerializer,
    PartnerCustomersQueryError,
    parse_partner_customers_query,
    prepare_customers_query,
    visible_customer_fields,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    apply_partner_role_header,
    partner_customers_role,
    resolve_partner_customers_channel,
)
from apps.billing.services.partner_customers import partner_customers_service
from apps.billing.services.partner_log import PartnerLegacyUsageMixin

_NO_STORE = "no-store"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_customers",
        summary="Partner channel customers",
        description=(
            "Current attributions for the caller's single team channel. "
            "An active member may read this route only. Field visibility and "
            "the default sort follow the resolved role: owner and admin "
            "receive credit_balance, total_partner_earned, and accrual_count; "
            "member omits total_partner_earned; viewer receives identity and "
            "attributed_at only. credit_balance is the personal Account "
            "balance cache, or null when that Account is missing. Forbidden "
            "fields are omitted. An explicit sort the role cannot see is "
            "invalid_sort."
        ),
        parameters=[
            OpenApiParameter("page", OpenApiTypes.INT, required=False),
            OpenApiParameter("page_size", OpenApiTypes.INT, required=False),
            OpenApiParameter(
                "sort",
                OpenApiTypes.STR,
                required=False,
                enum=["attributed_at", "total_partner_earned", "accrual_count"],
            ),
            OpenApiParameter(
                "order",
                OpenApiTypes.STR,
                required=False,
                enum=["asc", "desc"],
            ),
            OpenApiParameter("q", OpenApiTypes.STR, required=False),
        ],
        responses={
            200: OpenApiResponse(
                response=PartnerCustomersPageSerializer,
                description="Customer page",
            ),
            400: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description=(
                    "invalid_page, invalid_page_size, invalid_sort, "
                    "invalid_order, or invalid_query"
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
class PartnerCustomersView(PartnerLegacyUsageMixin, APIView):
    legacy_endpoint = "orgs.partner.customers"
    """Read customers. Auth, flag, summary resolver, then the list service."""

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _coded(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        return super().handle_exception(exc)

    def get(self, request: Request) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            channel = resolve_partner_customers_channel(request.user)
            role = partner_customers_role(request.user, channel)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        except PartnerContextAmbiguous:
            return _coded(status.HTTP_409_CONFLICT, "partner_context_ambiguous")
        try:
            query = prepare_customers_query(
                parse_partner_customers_query(request.query_params),
                role,
            )
        except PartnerCustomersQueryError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)

        page = partner_customers_service.list_customers(channel, query)
        response = Response(
            PartnerCustomersPageSerializer(
                {
                    "count": page.count,
                    "page": page.page,
                    "page_size": page.page_size,
                    "results": [
                        visible_customer_fields(row, role) for row in page.results
                    ],
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        apply_partner_role_header(response, role)
        return response
