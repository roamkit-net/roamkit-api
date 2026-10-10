"""POST /api/v1/billing/partner-grants/ (ADR 023)."""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    ParseError,
    UnsupportedMediaType,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.billing.exceptions import InsufficientFundsError, InvalidAmountError
from apps.billing.partner_channel import PartnerCreditGrant
from apps.billing.serializers import (
    PartnerGrantBodyError,
    PartnerGrantCodeSerializer,
    PartnerGrantRequestSerializer,
    PartnerGrantResponseSerializer,
    parse_partner_grant_body,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    PartnerContextAmbiguous,
    resolve_partner_grant_channel,
)
from apps.billing.services.partner_grant import (
    CustomerAttributionChanged,
    CustomerNotAttributed,
    PartnerChannelDisabled,
    PartnerGrantError,
    PartnerGrantForbidden,
    PartnerGrantIdempotencyConflict,
    PartnerGrantNegativeBalance,
    PartnerGrantSameAccount,
    partner_grant_service,
)
from apps.billing.services.partner_log import PartnerLegacyUsageMixin
from apps.billing.throttles import PartnerGrantRateThrottle
from core.http.request_id import get_or_create_request_id

_NO_STORE = "no-store"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


@extend_schema_view(
    post=extend_schema(
        tags=["Billing"],
        operation_id="billing_partner_grant",
        summary="Grant partner credits to an attributed customer",
        description=(
            "Move credits from the caller's single partner-channel team account "
            "to an attributed customer's personal account. The body is only "
            "customer_id, amount, and idempotency_key."
        ),
        request=PartnerGrantRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PartnerGrantResponseSerializer,
                description="Created grant, or the original grant on replay",
            ),
            400: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description="invalid_request or invalid_amount",
            ),
            401: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description="partner_access_denied or partner_grant_forbidden",
            ),
            404: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description="partner_channel_disabled or customer_not_found",
            ),
            409: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description=(
                    "partner_context_ambiguous, idempotency_key_conflict, "
                    "insufficient_funds, or customer_attribution_changed"
                ),
            ),
            429: OpenApiResponse(
                response=PartnerGrantCodeSerializer,
                description="rate_limited",
            ),
        },
    ),
)
class PartnerGrantView(PartnerLegacyUsageMixin, APIView):
    legacy_endpoint = "billing.partner_grants"
    """Grant credits. Tenant, body, and errors follow ADR 023."""

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _coded(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        if isinstance(exc, UnsupportedMediaType):
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")
        return super().handle_exception(exc)

    def post(self, request: Request) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")

        try:
            payload = request.data
        except ParseError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")
        try:
            parsed = parse_partner_grant_body(payload)
        except PartnerGrantBodyError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)

        try:
            channel = resolve_partner_grant_channel(request.user)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        except PartnerContextAmbiguous:
            return _coded(status.HTTP_409_CONFLICT, "partner_context_ambiguous")

        customer = User.objects.filter(pk=parsed.customer_id).first()
        if customer is None:
            return _coded(status.HTTP_404_NOT_FOUND, "customer_not_found")

        idempotency_key = parsed.idempotency_key
        if not _grant_exists(channel.pk, idempotency_key):
            self.partner_channel_id = channel.pk
            if not PartnerGrantRateThrottle().allow_request(request, self):
                return _coded(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")

        try:
            grant = partner_grant_service.grant(
                actor=request.user,
                partner_channel=channel,
                customer=customer,
                amount=parsed.amount,
                idempotency_key=idempotency_key,
                request_id=get_or_create_request_id(request),
            )
        except PartnerChannelDisabled:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        except PartnerGrantForbidden:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_grant_forbidden")
        except CustomerNotAttributed:
            return _coded(status.HTTP_404_NOT_FOUND, "customer_not_found")
        except CustomerAttributionChanged:
            return _coded(status.HTTP_409_CONFLICT, "customer_attribution_changed")
        except PartnerGrantIdempotencyConflict:
            return _coded(status.HTTP_409_CONFLICT, "idempotency_key_conflict")
        except PartnerGrantSameAccount:
            return _coded(status.HTTP_409_CONFLICT, "partner_grant_same_account")
        except (InsufficientFundsError, PartnerGrantNegativeBalance):
            return _coded(status.HTTP_409_CONFLICT, "insufficient_funds")
        except InvalidAmountError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_amount")
        except PartnerGrantError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")

        response = Response(
            PartnerGrantResponseSerializer(
                {
                    "grant_id": grant.pk,
                    "customer_id": grant.customer_user_id_snapshot,
                    "amount": grant.amount,
                    "created_at": grant.created_at,
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        return response


def _grant_exists(channel_id: object, idempotency_key: str) -> bool:
    return PartnerCreditGrant.objects.filter(
        partner_channel_id=channel_id,
        idempotency_key=idempotency_key,
    ).exists()
