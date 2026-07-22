"""Catalog API serializers."""

from rest_framework import serializers

from apps.catalog.models import Location, Package


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
            "voice_minutes",
            "text_sms",
        ]
        read_only_fields = fields


class LocationListSerializer(serializers.ModelSerializer):
    """Location card for the store listing."""

    min_price_usd = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True, read_only=True
    )

    class Meta:
        model = Location
        fields = [
            "slug",
            "title",
            "country_code",
            "coverage_type",
            "image_url",
            "covered_country_codes",
            "is_popular",
            "min_price_usd",
        ]
        read_only_fields = fields


class LocationSerializer(serializers.ModelSerializer):
    """Full location detail, optionally including broader coverage."""

    min_price_usd = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True, read_only=True
    )
    broader_locations = LocationListSerializer(many=True, read_only=True)

    class Meta:
        model = Location
        fields = [
            "slug",
            "title",
            "country_code",
            "coverage_type",
            "image_url",
            "covered_country_codes",
            "is_popular",
            "min_price_usd",
            "broader_locations",
        ]
        read_only_fields = fields
