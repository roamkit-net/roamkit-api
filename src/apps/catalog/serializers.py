"""Catalog API serializers."""

from rest_framework import serializers

from apps.catalog.models import Location, Package
from apps.pricing.presentation import resolve_package_quote
from apps.pricing.types import PricingQuote


class PackageSerializer(serializers.ModelSerializer):
    """Public package representation for the catalog API.

    Additive pricing (ADR 019): ``price_usd`` is the customer charge;
    ``list_price_usd`` is provider list. Anonymous / flag-off → retail.
    """

    id = serializers.CharField(source="external_id", read_only=True)
    price_usd = serializers.SerializerMethodField()
    list_price_usd = serializers.SerializerMethodField()
    discount_percent = serializers.SerializerMethodField()
    pricing_reason = serializers.SerializerMethodField()

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
            "list_price_usd",
            "discount_percent",
            "pricing_reason",
            "is_unlimited",
            "plan_type",
            "voice_minutes",
            "text_sms",
            "activation_policy",
        ]
        read_only_fields = fields

    def _quote(self, instance: Package) -> PricingQuote:
        cached = getattr(instance, "_resolved_pricing_quote", None)
        if cached is not None:
            return cached
        account = self.context.get("pricing_account")
        quote = resolve_package_quote(instance, account=account)
        instance._resolved_pricing_quote = quote
        return quote

    def get_price_usd(self, instance: Package) -> str:
        return format(self._quote(instance).customer_price, "f")

    def get_list_price_usd(self, instance: Package) -> str:
        return format(self._quote(instance).list_price, "f")

    def get_discount_percent(self, instance: Package) -> str:
        return format(self._quote(instance).discount_percent, "f")

    def get_pricing_reason(self, instance: Package) -> str:
        return self._quote(instance).pricing_reason


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
            "coverages",
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
            "coverages",
            "is_popular",
            "min_price_usd",
            "broader_locations",
        ]
        read_only_fields = fields
