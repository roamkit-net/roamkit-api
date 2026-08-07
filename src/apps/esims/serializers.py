"""eSIM API serializers."""

from datetime import datetime

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.esims.models import Esim, EsimAutoTopupPolicy, EsimLifecycleEvent, Topup

# Lifecycle event written by LifecycleService.transition(..., ACTIVATED).
ACTIVATED_EVENT_TYPE = "system.status.activated"


class EsimSerializer(serializers.ModelSerializer):
    """Owned eSIM with ICCID, install, setup, lifecycle, and order snapshot.

    ``note`` is the only writable field (PATCH). It is user-local metadata and
    is never synchronized to Airalo. Future RoamKit-only user metadata belongs
    on the same model with the same non-sync rule.
    """

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
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        help_text=(
            "User-local note on this eSIM. Never synchronized to Airalo. "
            "Future user metadata (label, favorite, archived, …) belongs on "
            "the same model without provider sync."
        ),
    )

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
            "note",
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
        read_only_fields = [
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

    def validate_note(self, value: str) -> str:
        return value.strip()

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
    """Available top-up package for an eSIM (additive pricing — ADR 019)."""

    id = serializers.CharField(source="external_id")
    title = serializers.CharField()
    data_allowance = serializers.CharField()
    validity_days = serializers.IntegerField()
    price_usd = serializers.DecimalField(max_digits=10, decimal_places=2)
    list_price_usd = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default="0.00"
    )
    discount_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, default="0.00"
    )
    pricing_reason = serializers.CharField(required=False, default="retail")
    is_unlimited = serializers.BooleanField()
    plan_type = serializers.CharField()

    def to_representation(self, instance) -> dict:
        from apps.pricing.presentation import public_price_dict, resolve_topup_quote

        data = {
            "id": instance.external_id,
            "title": instance.title,
            "data_allowance": instance.data_allowance,
            "validity_days": instance.validity_days,
            "is_unlimited": instance.is_unlimited,
            "plan_type": instance.plan_type,
        }
        account = self.context.get("pricing_account")
        quote = resolve_topup_quote(instance, account=account)
        for key, value in public_price_dict(quote).items():
            if key in {"price_usd", "list_price_usd", "discount_percent"}:
                data[key] = f"{value:.2f}"
            else:
                data[key] = value
        return data


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


class AutoTopupPolicySerializer(serializers.ModelSerializer):
    """Persisted auto top-up policy response (legacy trigger_mode until PR4)."""

    trigger_mode = serializers.SerializerMethodField()

    class Meta:
        model = EsimAutoTopupPolicy
        fields = [
            "id",
            "package_id",
            "enabled",
            "status",
            "reason",
            "trigger_mode",
            "threshold_mb",
            "renew_mode",
            "remaining_count",
            "cooldown_until",
            "last_triggered_at",
            "version",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_trigger_mode(self, obj: EsimAutoTopupPolicy) -> str:
        return obj.legacy_trigger_mode()


class AutoTopupPolicyWriteSerializer(serializers.Serializer):
    """PUT body; still accepts v1 trigger_mode mapped to v2 columns until PR4."""

    package_id = serializers.CharField(max_length=64)
    enabled = serializers.BooleanField(default=True)
    trigger_mode = serializers.ChoiceField(
        choices=[
            (EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_ZERO, "Usage zero"),
            (EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_THRESHOLD, "Usage threshold"),
            (EsimAutoTopupPolicy.LEGACY_TRIGGER_EXPIRY, "Expiry"),
        ]
    )
    threshold_mb = serializers.IntegerField(
        required=False, allow_null=True, min_value=1
    )
    renew_mode = serializers.ChoiceField(choices=EsimAutoTopupPolicy.RenewMode.choices)
    remaining_count = serializers.IntegerField(
        required=False, allow_null=True, min_value=0
    )
    version = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=0,
        help_text="Expected version for optimistic concurrency (or use If-Match).",
    )

    def validate(self, attrs):
        trigger = attrs.get("trigger_mode")
        renew = attrs.get("renew_mode")
        if trigger == EsimAutoTopupPolicy.LEGACY_TRIGGER_USAGE_THRESHOLD:
            if attrs.get("threshold_mb") is None:
                raise serializers.ValidationError(
                    {"threshold_mb": "Required when trigger_mode is usage_threshold."}
                )
        else:
            attrs["threshold_mb"] = attrs.get("threshold_mb")
        if renew == EsimAutoTopupPolicy.RenewMode.FIXED_COUNT:
            if attrs.get("remaining_count") is None:
                raise serializers.ValidationError(
                    {"remaining_count": "Required when renew_mode is fixed_count."}
                )
        else:
            attrs["remaining_count"] = attrs.get("remaining_count")
        return attrs
