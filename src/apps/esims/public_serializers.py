"""Serializers for POST /api/v1/public/esim/status/ (ADR 022)."""

from rest_framework import serializers


class CodedErrorSerializer(serializers.Serializer):
    detail = serializers.CharField()
    code = serializers.CharField()


class PublicEsimStatusRequestSerializer(serializers.Serializer):
    matching_id = serializers.CharField(required=False, allow_blank=True)


class PublicCoverageSummarySerializer(serializers.Serializer):
    available = serializers.BooleanField()
    country_count = serializers.IntegerField()


class PublicPlanSerializer(serializers.Serializer):
    title = serializers.CharField(allow_null=True)
    data_allowance = serializers.CharField(allow_null=True)
    validity_days = serializers.IntegerField(allow_null=True)
    country_code = serializers.CharField(allow_null=True)
    coverage_type = serializers.CharField(allow_null=True)
    location_title = serializers.CharField(allow_null=True)
    coverage_summary = PublicCoverageSummarySerializer(allow_null=True)


class PublicEsimSerializer(serializers.Serializer):
    iccid = serializers.CharField()
    status = serializers.CharField()


class PublicUsageSerializer(serializers.Serializer):
    data_remaining = serializers.CharField(allow_null=True)
    data_used = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)
    synced_at = serializers.DateTimeField(allow_null=True)


class PublicAutoTopupSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()


class PublicCoverageCountrySerializer(serializers.Serializer):
    country_code = serializers.CharField()
    country_name = serializers.CharField(allow_null=True)
    operators = serializers.ListField(child=serializers.CharField(), allow_empty=True)


class PublicCoverageSerializer(serializers.Serializer):
    coverage_type = serializers.CharField(allow_null=True)
    coverage = PublicCoverageCountrySerializer(many=True)


class PublicEsimStatusSerializer(serializers.Serializer):
    esim = PublicEsimSerializer()
    usage = PublicUsageSerializer(allow_null=True)
    auto_topup = PublicAutoTopupSerializer()
    plan = PublicPlanSerializer(allow_null=True)
    packages = serializers.JSONField(allow_null=True)
    coverage = PublicCoverageSerializer(allow_null=True)
    checked_at = serializers.DateTimeField()
