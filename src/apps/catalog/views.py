"""Catalog API views."""

from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny

from apps.catalog.models import Package
from apps.catalog.serializers import PackageSerializer


class PackageListView(ListAPIView):
    """List active packages synced from external providers."""

    serializer_class = PackageSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = Package.objects.filter(is_active=True)
        country = self.request.query_params.get("country")
        if country:
            queryset = queryset.filter(country_code__iexact=country.strip())
        return queryset
