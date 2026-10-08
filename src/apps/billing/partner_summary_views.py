"""GET /api/v1/orgs/partner/summary/ (ADR 023)."""

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
    PartnerSummarySerializer,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    resolve_partner_summary_channel,
)
from apps.billing.services.partner_summary import partner_summary_service

_NO_STORE = "no-store"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="orgs_partner_summary",
        summary="Partner channel summary",
        description=(
            "Stored margin total, display balance, and accrual counts for the "
            "caller's single partner channel."
        ),
        responses={
            200: OpenApiResponse(
                response=PartnerSummarySerializer,
                description="Channel summary",
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
class PartnerSummaryView(APIView):
    """Read summary. Auth, then the summary resolver, then the service."""

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

        summary = partner_summary_service.summarize(channel)
        counts = summary.accrual_counts
        response = Response(
            PartnerSummarySerializer(
                {
                    "total_earned": summary.total_earned,
                    "available_balance": summary.available_balance,
                    "accrual_counts": {
                        "order": counts.order,
                        "topup": counts.topup,
                        "subscription": counts.subscription,
                        "total": counts.total,
                    },
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        return response
