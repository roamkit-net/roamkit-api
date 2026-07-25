"""My eSIM API views."""

from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import GenericAPIView, ListAPIView, RetrieveAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.exceptions import (
    BillingDisabledError,
    InsufficientFundsError,
    InvalidAmountError,
)
from apps.esims.exceptions import TopupPackageNotFoundError
from apps.esims.models import Esim
from apps.esims.serializers import (
    EsimSerializer,
    PurchaseTopupSerializer,
    TopupPackageSerializer,
    TopupSerializer,
    UsageSerializer,
)
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from apps.orders.exceptions import IdempotencyKeyRequiredError, SpendInProgressError
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
    """List or purchase top-up packages for an owned eSIM."""

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        packages = TopupService(get_topup_provider()).list_topups(esim)
        return Response({"results": TopupPackageSerializer(packages, many=True).data})

    def post(self, request: Request, *args, **kwargs) -> Response:
        if not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")

        esim = self.get_object()
        serializer = PurchaseTopupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        service = TopupService(get_topup_provider())
        try:
            topup = service.purchase(
                esim,
                package_id=serializer.validated_data["package_id"],
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except BillingDisabledError as exc:
            raise NotFound(detail="Not found.") from exc
        except SpendInProgressError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_409_CONFLICT,
            )
        except IdempotencyKeyRequiredError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except TopupPackageNotFoundError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except InsufficientFundsError as exc:
            return Response(
                exc.to_api_dict(),
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        except InvalidAmountError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(TopupSerializer(topup).data, status=status.HTTP_201_CREATED)
