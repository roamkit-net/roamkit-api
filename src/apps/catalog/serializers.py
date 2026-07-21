"""Catalog API serializers."""

from rest_framework import serializers

from apps.catalog.models import Package


class PackageSerializer(serializers.ModelSerializer):
    """Public package representation for the catalog API."""

    id = serializers.CharField(source="external_id", read_only=True)

    class Meta:
        model = Package
        fields = [
            "id",
            "title",
            "operator_title",
            "country_code",
            "data_allowance",
            "validity_days",
            "price_usd",
            "is_unlimited",
            "plan_type",
        ]
        read_only_fields = fields
