"""eSIM API serializers."""

from rest_framework import serializers

from apps.esims.models import Esim, Topup


class EsimSerializer(serializers.ModelSerializer):
    """Owned eSIM with ICCID and installation / QR fields."""

    class Meta:
        model = Esim
        fields = [
            "id",
            "iccid",
            "lpa",
            "matching_id",
            "qrcode",
            "qrcode_url",
            "direct_apple_installation_url",
            "manual_installation",
            "qrcode_installation",
            "installation_guide_url",
            "status",
            "usage_remaining_mb",
            "usage_total_mb",
            "usage_status",
            "usage_is_unlimited",
            "usage_expired_at",
            "usage_synced_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class UsageSerializer(serializers.Serializer):
    """Live usage snapshot from the provider."""

    remaining_mb = serializers.IntegerField()
    total_mb = serializers.IntegerField()
    expired_at = serializers.CharField(allow_null=True)
    is_unlimited = serializers.BooleanField(allow_null=True)
    status = serializers.CharField()
    remaining_voice = serializers.IntegerField()
    remaining_text = serializers.IntegerField()
    total_voice = serializers.IntegerField()
    total_text = serializers.IntegerField()


class TopupPackageSerializer(serializers.Serializer):
    """Available top-up package for an eSIM."""

    id = serializers.CharField(source="external_id")
    title = serializers.CharField()
    data_allowance = serializers.CharField()
    validity_days = serializers.IntegerField()
    price_usd = serializers.DecimalField(max_digits=10, decimal_places=2)
    is_unlimited = serializers.BooleanField()
    plan_type = serializers.CharField()


class PurchaseTopupSerializer(serializers.Serializer):
    """Request body for POST /api/v1/me/esims/{id}/topups/."""

    package_id = serializers.CharField(max_length=64)
    idempotency_key = serializers.CharField(max_length=128)


class TopupSerializer(serializers.ModelSerializer):
    """Persisted top-up purchase response."""

    class Meta:
        model = Topup
        fields = [
            "id",
            "package_external_id",
            "amount",
            "status",
            "external_order_id",
            "idempotency_key",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields
