"""Order HTTP API serializers."""

from rest_framework import serializers

from apps.esims.serializers import EsimSerializer
from apps.orders.models import Order


class CreateOrderSerializer(serializers.Serializer):
    """Request body for POST /api/v1/orders/."""

    package_id = serializers.CharField(max_length=64)
    idempotency_key = serializers.CharField(max_length=128)


class OrderSerializer(serializers.ModelSerializer):
    """Order response including provisioned eSIMs when fulfilled.

    Exposes purchase-time product snapshot (``paid_usd``, titles, …) for the
    buyer. Never includes wholesale ``net_price_usd``.
    """

    package_id = serializers.CharField(source="package.external_id", read_only=True)
    paid_usd = serializers.DecimalField(
        source="retail_price_usd",
        max_digits=10,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    esims = EsimSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            "id",
            "package_id",
            "status",
            "external_order_id",
            "customer_ref",
            "idempotency_key",
            "package_title",
            "location_title",
            "country_code",
            "data_allowance",
            "validity_days",
            "paid_usd",
            "currency",
            "esims",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
