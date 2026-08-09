"""Order HTTP API — POST /api/v1/orders/ (debit then fulfill)."""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.exceptions import (
    BillingDisabledError,
    InsufficientFundsError,
    InvalidAmountError,
)
from apps.catalog.models import Package
from apps.orders.exceptions import IdempotencyKeyRequiredError, SpendInProgressError
from apps.orders.serializers import CreateOrderSerializer, OrderSerializer
from apps.orders.services.order_service import OrderService
from apps.organizations.services import require_spend, resolve_account_context
from core.openapi_serializers import (
    ErrorDetailSerializer,
    InsufficientCreditsSerializer,
)
from shared.providers.factory import get_order_provider


@extend_schema_view(
    post=extend_schema(
        tags=["Orders"],
        operation_id="orders_create",
        summary="Create package order",
        description=(
            "Debit prepaid credits then fulfill via the order provider. "
            "Idempotent on ``idempotency_key`` "
            "(replay returns 201 with the same body). "
            "Omit ``organization_id`` for personal Account spend; set it to "
            "purchase against a team Account "
            "(requires active membership with ``can_spend``). "
            "Client-supplied ``account_id`` is never accepted."
        ),
        request=CreateOrderSerializer,
        examples=[
            OpenApiExample(
                "Create order (personal)",
                value={
                    "package_id": "airalo-pkg-123",
                    "idempotency_key": "client-order-uuid-1",
                },
                request_only=True,
            ),
            OpenApiExample(
                "Create order (team)",
                value={
                    "package_id": "airalo-pkg-123",
                    "idempotency_key": "client-order-uuid-2",
                    "organization_id": "11111111-1111-1111-1111-111111111111",
                },
                request_only=True,
            ),
        ],
        responses={
            201: OpenApiResponse(response=OrderSerializer, description="Order created"),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            402: OpenApiResponse(
                response=InsufficientCreditsSerializer,
                description="Insufficient credits",
                examples=[
                    OpenApiExample(
                        "Insufficient credits",
                        value={
                            "code": "INSUFFICIENT_CREDITS",
                            "detail": "Insufficient funds",
                            "required": "19.500000",
                            "balance": "5.000000",
                            "missing": "14.500000",
                        },
                    )
                ],
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Not allowed to spend in organization context",
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Package/organization not found or billing disabled",
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer, description="Spend already in progress"
            ),
        },
    ),
)
class OrderCreateView(APIView):
    """POST /api/v1/orders/ — debit prepaid credits then fulfill via provider."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        if not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")

        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        package_id = serializer.validated_data["package_id"]
        idempotency_key = serializer.validated_data["idempotency_key"]
        organization_id = serializer.validated_data.get("organization_id")

        context = resolve_account_context(request.user, organization_id=organization_id)
        if context.kind == "organization":
            require_spend(context)

        try:
            package = Package.objects.get(external_id=package_id, is_active=True)
        except Package.DoesNotExist as exc:
            raise NotFound(detail="Package not found.") from exc

        service = OrderService(get_order_provider())
        try:
            order = service.fulfill(
                user=request.user,
                package=package,
                idempotency_key=idempotency_key,
                account=context.account,
            )
        except BillingDisabledError as exc:
            raise NotFound(detail="Not found.") from exc
        except SpendInProgressError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except IdempotencyKeyRequiredError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except InsufficientFundsError as exc:
            return Response(
                exc.to_api_dict(),
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        except InvalidAmountError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Idempotent replay of a completed request still returns 201 with body.
        return Response(
            OrderSerializer(order).data,
            status=status.HTTP_201_CREATED,
        )
