"""My eSIM API views."""

from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from apps.esims.models import Esim
from apps.esims.serializers import (
    EsimSerializer,
    TopupPackageSerializer,
    UsageSerializer,
)
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from shared.providers.factory import get_topup_provider


class OwnedEsimMixin:
    """Scopes eSIM lookups to the authenticated owner (404 for others)."""

    def get_queryset(self):
        return Esim.objects.filter(user=self.request.user)


class EsimListView(OwnedEsimMixin, ListAPIView):
    """List eSIMs owned by the authenticated user."""

    serializer_class = EsimSerializer


class EsimDetailView(OwnedEsimMixin, RetrieveAPIView):
    """Retrieve a single owned eSIM."""

    serializer_class = EsimSerializer


class EsimUsageView(OwnedEsimMixin, GenericAPIView):
    """Fetch live usage for an owned eSIM and refresh the cache."""

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        usage = UsageService(get_topup_provider()).get_usage(esim)
        return Response(UsageSerializer(usage).data)


class EsimTopupsView(OwnedEsimMixin, GenericAPIView):
    """List available top-up packages for an owned eSIM (no purchase)."""

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        packages = TopupService(get_topup_provider()).list_topups(esim)
        return Response({"results": TopupPackageSerializer(packages, many=True).data})
