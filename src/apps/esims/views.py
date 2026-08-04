"""My eSIM API views."""

from django.conf import settings
from django.db.models import Prefetch
from drf_spectacular.utils import (
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.generics import (
    GenericAPIView,
    ListAPIView,
    RetrieveUpdateAPIView,
)
from rest_framework.request import Request
from rest_framework.response import Response

from apps.billing.exceptions import (
    BillingDisabledError,
    InsufficientFundsError,
    InvalidAmountError,
)
from apps.esims.exceptions import (
    TopupPackageNotFoundError,
    UnknownLifecycleEventTypeError,
)
from apps.esims.models import Esim, EsimLifecycleEvent
from apps.esims.serializers import (
    ACTIVATED_EVENT_TYPE,
    EsimSerializer,
    LifecycleEventCreateSerializer,
    LifecycleEventSerializer,
    PurchaseTopupSerializer,
    TopupPackageSerializer,
    TopupSerializer,
    UsageSerializer,
)
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from apps.orders.exceptions import (
    IdempotencyKeyRequiredError,
    ProviderFulfillmentError,
    SpendInProgressError,
)
from core.openapi_serializers import (
    ErrorDetailSerializer,
    InsufficientCreditsSerializer,
)
from shared.providers.factory import get_topup_provider


class OwnedEsimMixin:
    """Scopes eSIM lookups to the authenticated owner (404 for others)."""

    def get_queryset(self):
        return (
            Esim.objects.filter(user=self.request.user)
            .select_related("order")
            .prefetch_related(
                Prefetch(
                    "lifecycle_events",
                    queryset=EsimLifecycleEvent.objects.filter(
                        event_type=ACTIVATED_EVENT_TYPE
                    ).order_by("created_at"),
                    to_attr="_activated_events",
                )
            )
        )


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_list",
        summary="List my eSIMs",
        description="List eSIMs owned by the authenticated user.",
        responses={
            200: OpenApiResponse(
                response=EsimSerializer(many=True), description="Paginated eSIMs"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
        },
    ),
)
class EsimListView(OwnedEsimMixin, ListAPIView):
    """List eSIMs owned by the authenticated user."""

    serializer_class = EsimSerializer


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_retrieve",
        summary="Retrieve my eSIM",
        description="Retrieve a single eSIM owned by the authenticated user.",
        responses={
            200: OpenApiResponse(response=EsimSerializer, description="eSIM"),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
    patch=extend_schema(
        tags=["eSIM"],
        operation_id="esim_partial_update",
        summary="Update my eSIM note",
        description=(
            "Partially update an owned eSIM. Only ``note`` is writable. "
            "``note`` is user-local metadata and is never synchronized to Airalo. "
            "Auth-gated like other My eSIM endpoints (no dedicated throttle). "
            "PUT is not supported."
        ),
        request=EsimSerializer,
        responses={
            200: OpenApiResponse(response=EsimSerializer, description="eSIM"),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
)
class EsimDetailView(OwnedEsimMixin, RetrieveUpdateAPIView):
    """Retrieve or partially update a single owned eSIM (PATCH note only)."""

    serializer_class = EsimSerializer
    http_method_names = ["get", "head", "options", "patch"]


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_usage",
        summary="eSIM usage",
        description="Fetch live usage for an owned eSIM and refresh the cache.",
        responses={
            200: OpenApiResponse(
                response=UsageSerializer, description="Usage snapshot"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
)
class EsimUsageView(OwnedEsimMixin, GenericAPIView):
    """Fetch live usage for an owned eSIM and refresh the cache."""

    serializer_class = UsageSerializer

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        usage = UsageService(get_topup_provider()).get_usage(esim)
        return Response(UsageSerializer(usage).data)


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_events_list",
        summary="List eSIM lifecycle events",
        description="Chronological install / lifecycle trail for an owned eSIM.",
        responses={
            200: OpenApiResponse(
                response=LifecycleEventSerializer(many=True),
                description="Lifecycle events",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
    post=extend_schema(
        tags=["eSIM"],
        operation_id="esim_events_create",
        summary="Record eSIM lifecycle event",
        description=(
            "Record a client install/telemetry event. Idempotent on "
            "``idempotency_key`` per eSIM."
        ),
        request=LifecycleEventCreateSerializer,
        responses={
            200: OpenApiResponse(
                response=LifecycleEventSerializer,
                description="Existing event (idempotent replay)",
            ),
            201: OpenApiResponse(
                response=LifecycleEventSerializer, description="Event created"
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
)
class EsimEventsView(OwnedEsimMixin, GenericAPIView):
    """List or record lifecycle events for an owned eSIM."""

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        events = esim.lifecycle_events.all()
        return Response(LifecycleEventSerializer(events, many=True).data)

    def post(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        serializer = LifecycleEventCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            event, created = lifecycle_service.record_client_event(
                esim,
                event_type=data["event_type"],
                idempotency_key=data["idempotency_key"],
                schema_version=data.get("schema_version", 1),
                setup_session_id=data.get("setup_session_id"),
                payload=data.get("payload") or {},
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
                resume_step=data.get("resume_step"),
            )
        except UnknownLifecycleEventTypeError as exc:
            return Response(
                {"detail": f"Unknown event_type: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            LifecycleEventSerializer(event).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_topups_list",
        summary="List top-up packages",
        description="List available top-up packages for an owned eSIM.",
        responses={
            200: OpenApiResponse(description="Top-up package list"),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
    post=extend_schema(
        tags=["eSIM"],
        operation_id="esim_topups_purchase",
        summary="Purchase top-up",
        description=(
            "Purchase a top-up package using prepaid credits. "
            "Idempotent on ``idempotency_key``."
        ),
        request=PurchaseTopupSerializer,
        responses={
            201: OpenApiResponse(
                response=TopupSerializer, description="Top-up created"
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            402: OpenApiResponse(
                response=InsufficientCreditsSerializer,
                description="Insufficient credits",
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="eSIM/package not found or billing disabled",
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer, description="Spend already in progress"
            ),
        },
    ),
)
class EsimTopupsView(OwnedEsimMixin, GenericAPIView):
    """List or purchase top-up packages for an owned eSIM."""

    serializer_class = PurchaseTopupSerializer

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
        except ProviderFulfillmentError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(TopupSerializer(topup).data, status=status.HTTP_201_CREATED)
