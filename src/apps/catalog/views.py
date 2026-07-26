"""Catalog API views."""

from django.db.models import Min, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny

from apps.catalog.location_slugs import resolve_location_slug
from apps.catalog.models import Location, Package
from apps.catalog.serializers import (
    LocationListSerializer,
    LocationSerializer,
    PackageSerializer,
)


def _active_package_min_price():
    return Min("packages__price_usd", filter=Q(packages__is_active=True))


@extend_schema_view(
    get=extend_schema(
        tags=["Catalog"],
        operation_id="catalog_packages_list",
        summary="List packages",
        description="List active packages synced from external providers.",
        auth=[],
        parameters=[
            OpenApiParameter(
                name="country",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by ISO country code",
            ),
            OpenApiParameter(
                name="location",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by location slug",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=PackageSerializer(many=True), description="Paginated packages"
            ),
        },
    ),
)
class PackageListView(ListAPIView):
    """List active packages synced from external providers."""

    serializer_class = PackageSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = Package.objects.filter(is_active=True)
        country = self.request.query_params.get("country")
        if country:
            queryset = queryset.filter(country_code__iexact=country.strip())
        location = self.request.query_params.get("location")
        if location:
            queryset = queryset.filter(
                location__slug__iexact=resolve_location_slug(location)
            )
        return queryset


@extend_schema_view(
    get=extend_schema(
        tags=["Catalog"],
        operation_id="catalog_locations_list",
        summary="List locations",
        description="List catalog locations that have at least one active package.",
        auth=[],
        parameters=[
            OpenApiParameter(
                name="type",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter: popular | local | regional | global",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=LocationListSerializer(many=True),
                description="Paginated locations",
            ),
        },
    ),
)
class LocationListView(ListAPIView):
    """List catalog locations with optional coverage-type filters."""

    serializer_class = LocationListSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = (
            Location.objects.annotate(min_price_usd=_active_package_min_price())
            .filter(min_price_usd__isnull=False)
            .order_by("title")
        )

        location_type = (self.request.query_params.get("type") or "").strip().lower()
        if location_type == "popular":
            queryset = queryset.filter(is_popular=True)
        elif location_type in {
            Location.COVERAGE_LOCAL,
            Location.COVERAGE_REGIONAL,
            Location.COVERAGE_GLOBAL,
        }:
            queryset = queryset.filter(coverage_type=location_type)

        return queryset


@extend_schema_view(
    get=extend_schema(
        tags=["Catalog"],
        operation_id="catalog_locations_retrieve",
        summary="Retrieve location",
        description=(
            "Retrieve a single location by slug, including broader coverage options."
        ),
        auth=[],
        responses={
            200: OpenApiResponse(response=LocationSerializer, description="Location"),
            404: OpenApiResponse(description="Location not found"),
        },
    ),
)
class LocationDetailView(RetrieveAPIView):
    """Retrieve a single location by slug, with broader coverage options."""

    serializer_class = LocationSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"
    lookup_url_kwarg = "slug"

    def get_queryset(self):
        return Location.objects.annotate(min_price_usd=_active_package_min_price())

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        raw_slug = self.kwargs[self.lookup_url_kwarg]
        slug = resolve_location_slug(raw_slug)
        location = get_object_or_404(queryset, slug__iexact=slug)
        location.broader_locations = self._broader_locations(location)
        return location

    def _broader_locations(self, location: Location) -> list[Location]:
        is_local = location.coverage_type == Location.COVERAGE_LOCAL
        if not is_local or not location.country_code:
            return []

        country = location.country_code.upper()
        # Prefer DB-side JSON containment when the backend supports it; fall back
        # to a Python filter so SQLite tests still work.
        candidates = list(
            Location.objects.annotate(min_price_usd=_active_package_min_price())
            .filter(
                coverage_type__in=[
                    Location.COVERAGE_REGIONAL,
                    Location.COVERAGE_GLOBAL,
                ],
                min_price_usd__isnull=False,
            )
            .exclude(pk=location.pk)
            .order_by("coverage_type", "title")
        )
        return [
            candidate
            for candidate in candidates
            if country in (candidate.covered_country_codes or [])
        ]
