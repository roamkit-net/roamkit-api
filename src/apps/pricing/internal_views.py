"""Internal pricing preview API (ADR 019 PR4).

``POST /api/internal/pricing/preview`` — staff-only, read-only resolve.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import Account
from apps.catalog.models import Package
from apps.ops.permissions import IsStaff
from apps.pricing.presentation import internal_preview_dict, resolve_preview_quote
from apps.pricing.types import OrderType


class PricingPreviewRequestSerializer(serializers.Serializer):
    """Preview inputs — package catalog id and/or explicit prices for topup."""

    order_type = serializers.ChoiceField(
        choices=[OrderType.PACKAGE, OrderType.TOPUP],
        default=OrderType.PACKAGE,
    )
    account_id = serializers.UUIDField(required=False, allow_null=True)
    package_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
        help_text="Catalog ``Package.external_id`` when order_type=package.",
    )
    list_price_usd = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
    )
    net_price_usd = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        allow_null=True,
    )

    def validate(self, attrs: dict) -> dict:
        order_type = attrs.get("order_type") or OrderType.PACKAGE
        package_id = (attrs.get("package_id") or "").strip()
        list_price = attrs.get("list_price_usd")
        if order_type == OrderType.PACKAGE:
            if not package_id:
                raise serializers.ValidationError(
                    {"package_id": "Required when order_type is package."}
                )
        else:
            if list_price is None and not package_id:
                raise serializers.ValidationError(
                    {
                        "list_price_usd": (
                            "Provide list_price_usd or package_id for topup preview."
                        )
                    }
                )
        return attrs


class PricingPreviewResponseSerializer(serializers.Serializer):
    """Internal preview response (may include ops-only fields)."""

    price_usd = serializers.DecimalField(max_digits=10, decimal_places=2)
    list_price_usd = serializers.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = serializers.DecimalField(max_digits=5, decimal_places=2)
    pricing_reason = serializers.CharField()
    floor_reason = serializers.CharField()
    pricing_profile_id = serializers.UUIDField(allow_null=True)
    pricing_profile_version = serializers.IntegerField(allow_null=True)
    pricing_profile_slug = serializers.CharField(allow_null=True, allow_blank=True)
    pricing_context_hash = serializers.CharField()
    quote_fingerprint = serializers.CharField()
    snapshot_schema_version = serializers.IntegerField()


class PricingPreviewView(APIView):
    """Staff-only pricing preview — ``PricingService.resolve`` only."""

    permission_classes = [IsStaff]

    @extend_schema(
        tags=["Internal"],
        operation_id="internal_pricing_preview",
        summary="Preview pricing quote",
        description=(
            "Read-only preview via ``PricingService.resolve``. "
            "No ledger writes, no cache mutation, no side effects."
        ),
        request=PricingPreviewRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PricingPreviewResponseSerializer,
                description="Resolved quote",
            ),
            400: OpenApiResponse(description="Invalid request"),
            403: OpenApiResponse(description="Staff required"),
            404: OpenApiResponse(description="Package or account not found"),
        },
    )
    def post(self, request: Request) -> Response:
        ser = PricingPreviewRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        account = None
        account_id: UUID | None = data.get("account_id")
        if account_id is not None:
            account = get_object_or_404(
                Account.objects.select_related("pricing_profile"),
                pk=account_id,
            )

        order_type = data.get("order_type") or OrderType.PACKAGE
        package_id = (data.get("package_id") or "").strip()
        list_price: Decimal | None = data.get("list_price_usd")
        net_price: Decimal | None = data.get("net_price_usd")

        if package_id and order_type == OrderType.PACKAGE:
            package = get_object_or_404(
                Package.objects.filter(is_active=True),
                external_id=package_id,
            )
            list_price = package.price_usd
            net_price = package.net_price_usd
        elif package_id and list_price is None:
            # Topup: optional catalog package as list/net source when present.
            package = get_object_or_404(
                Package.objects.filter(is_active=True),
                external_id=package_id,
            )
            list_price = package.price_usd
            if net_price is None:
                net_price = package.net_price_usd

        if list_price is None:
            return Response(
                {"list_price_usd": ["This field is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        quote = resolve_preview_quote(
            list_price=list_price,
            net_price=net_price,
            order_type=order_type,
            account=account,
        )
        payload = internal_preview_dict(quote)
        # JSON-friendly decimals
        for key in ("price_usd", "list_price_usd", "discount_percent"):
            payload[key] = f"{payload[key]:.2f}"
        return Response(payload, status=status.HTTP_200_OK)
