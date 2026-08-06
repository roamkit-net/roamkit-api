"""DRF serializers for OpenAPI documentation of ops DTOs."""

from __future__ import annotations

from rest_framework import serializers


class OpsEventSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    type = serializers.CharField()
    timestamp = serializers.DateTimeField()
    title = serializers.CharField()
    subtitle = serializers.CharField(allow_blank=True)
    reference_id = serializers.CharField(allow_blank=True)
    severity = serializers.ChoiceField(choices=["info", "warning", "error"])
    event_group = serializers.ChoiceField(
        choices=["account", "billing", "order", "esim", "wallet", "voucher"]
    )
    icon = serializers.CharField()
    user_id = serializers.IntegerField(allow_null=True, required=False)
    user_email = serializers.EmailField(allow_null=True, required=False)


class HealthItemSerializer(serializers.Serializer):
    status = serializers.CharField()
    reason = serializers.CharField()
    message = serializers.CharField()
    checked_at = serializers.CharField()
    source = serializers.CharField()
    timeout_ms = serializers.IntegerField()
    latency_ms = serializers.IntegerField(allow_null=True, required=False)
    last_success_at = serializers.CharField(allow_null=True, required=False)
    cache = serializers.DictField(required=False)
    details = serializers.DictField()


class OpsHealthMetricSerializer(serializers.Serializer):
    key = serializers.CharField()
    value = serializers.FloatField(allow_null=True)
    unit = serializers.CharField()
    status = serializers.CharField()


class OpsHealthSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    overall_status = serializers.CharField()
    generated_at = serializers.CharField()
    version = serializers.DictField()
    dependencies = serializers.DictField(child=HealthItemSerializer())
    workers = serializers.DictField(child=HealthItemSerializer())
    providers = serializers.DictField(child=HealthItemSerializer())
    metrics = OpsHealthMetricSerializer(many=True)
    checks = serializers.DictField(child=HealthItemSerializer())


class OpsAlertSerializer(serializers.Serializer):
    code = serializers.CharField()
    severity = serializers.CharField()
    title = serializers.CharField()
    count = serializers.IntegerField()


class OpsDashboardSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    kpi = serializers.DictField()
    pending_work = serializers.DictField()
    financial = serializers.DictField()
    top_destinations = serializers.ListField()
    top_packages = serializers.ListField()
    alerts = OpsAlertSerializer(many=True)
    health = OpsHealthSerializer()
    activity = OpsEventSerializer(many=True)


class OpsSearchHitUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    match = serializers.CharField()


class OpsSearchHitOrderSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    status = serializers.CharField()
    user_id = serializers.IntegerField()
    user_email = serializers.EmailField()
    match = serializers.CharField()


class OpsSearchHitDepositSerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()
    status = serializers.CharField()
    user_id = serializers.IntegerField()
    user_email = serializers.EmailField()
    match = serializers.CharField()


class OpsSearchHitEsimSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    label = serializers.CharField()
    status = serializers.CharField()
    user_id = serializers.IntegerField()
    user_email = serializers.EmailField()
    match = serializers.CharField()


class OpsSearchHitVoucherSerializer(serializers.Serializer):
    id = serializers.CharField()
    label = serializers.CharField()
    status = serializers.CharField()
    match = serializers.CharField()


class OpsSearchResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    query = serializers.CharField()
    users = OpsSearchHitUserSerializer(many=True)
    orders = OpsSearchHitOrderSerializer(many=True)
    deposits = OpsSearchHitDepositSerializer(many=True)
    esims = OpsSearchHitEsimSerializer(many=True)
    vouchers = OpsSearchHitVoucherSerializer(many=True)


class OpsUserListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    is_active = serializers.BooleanField()
    is_staff = serializers.BooleanField()
    last_login = serializers.DateTimeField(allow_null=True)
    balance = serializers.CharField(allow_null=True)
    badges = serializers.ListField(child=serializers.CharField())


class OpsUserDetailSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    id = serializers.IntegerField()
    email = serializers.EmailField()
    is_active = serializers.BooleanField()
    is_staff = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    last_login = serializers.DateTimeField(allow_null=True)
    badges = serializers.ListField(child=serializers.CharField())
    account = serializers.DictField(allow_null=True)
    esims = serializers.ListField()
    wallet = serializers.DictField(allow_null=True)
    device_hints = serializers.DictField()
    timeline = OpsEventSerializer(many=True)


class OpsOrderListItemSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField()
    package_title = serializers.CharField()
    retail_price_usd = serializers.CharField(allow_null=True)
    country_code = serializers.CharField()
    user_id = serializers.IntegerField()
    user_email = serializers.EmailField()
    created_at = serializers.DateTimeField()


class OpsDepositListItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    status = serializers.CharField()
    amount_requested = serializers.CharField()
    amount_credited = serializers.CharField(allow_null=True)
    payment_method = serializers.CharField()
    tx_hash = serializers.CharField(allow_null=True, allow_blank=True)
    user_id = serializers.IntegerField()
    user_email = serializers.EmailField()
    created_at = serializers.DateTimeField()
