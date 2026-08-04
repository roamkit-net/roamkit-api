"""eSIM API serializers."""

from datetime import datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.esims.models import Esim, EsimLifecycleEvent, Topup

# Lifecycle event written by LifecycleService.transition(..., ACTIVATED).
ACTIVATED_EVENT_TYPE = "system.status.activated"


class EsimSerializer(serializers.ModelSerializer):
    """Owned eSIM with ICCID, install, setup, lifecycle, and order snapshot."""

    package_title = serializers.CharField(source="order.package_title", read_only=True)
    location_title = serializers.CharField(
        source="order.location_title", read_only=True
    )
    country_code = serializers.CharField(source="order.country_code", read_only=True)
    data_allowance = serializers.CharField(
        source="order.data_allowance", read_only=True
    )
    validity_days = serializers.IntegerField(
        source="order.validity_days", read_only=True, allow_null=True
    )
    paid_usd = serializers.DecimalField(
        source="order.retail_price_usd",
        max_digits=10,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )
    currency = serializers.CharField(source="order.currency", read_only=True)
    issued_at = serializers.DateTimeField(source="created_at", read_only=True)
    activated_at = serializers.SerializerMethodField()

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
            "activation_policy",
            "setup_version",
            "setup_resume_step",
            "setup_completed_at",
            "setup_skipped_at",
            "usage_remaining_mb",
            "usage_total_mb",
            "usage_status",
            "usage_is_unlimited",
            "usage_expired_at",
            "usage_synced_at",
            "package_title",
            "location_title",
            "country_code",
            "data_allowance",
            "validity_days",
            "paid_usd",
            "currency",
            "issued_at",
            "activated_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    @extend_schema_field(OpenApiTypes.DATETIME)
    def get_activated_at(self, obj: Esim) -> datetime | None:
        """First ``system.status.activated`` event time, or null."""
        prefetched = getattr(obj, "_activated_events", None)
        if prefetched is not None:
            return prefetched[0].created_at if prefetched else None
        event = (
            obj.lifecycle_events.filter(event_type=ACTIVATED_EVENT_TYPE)
            .order_by("created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        return event


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


class LifecycleEventCreateSerializer(serializers.Serializer):
    """Request body for POST /api/v1/me/esims/{id}/events/."""

    event_type = serializers.CharField(max_length=64)
    idempotency_key = serializers.CharField(max_length=128)
    setup_session_id = serializers.UUIDField(required=False, allow_null=True)
    schema_version = serializers.IntegerField(required=False, default=1, min_value=1)
    payload = serializers.DictField(required=False, default=dict)
    resume_step = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=4
    )


class LifecycleEventSerializer(serializers.ModelSerializer):
    """Persisted lifecycle / install telemetry event."""

    class Meta:
        model = EsimLifecycleEvent
        fields = [
            "id",
            "event_type",
            "source",
            "schema_version",
            "idempotency_key",
            "setup_session_id",
            "payload",
            "created_at",
        ]
        read_only_fields = fields


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
