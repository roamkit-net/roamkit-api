"""Order HTTP API — POST /api/v1/orders/ (debit then fulfill)."""

from __future__ import annotations

from django.conf import settings
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
from shared.providers.factory import get_order_provider


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
