"""My eSIM API views."""

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
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
from apps.esims.models import Esim, EsimAutoTopupPolicy, EsimLifecycleEvent
from apps.esims.serializers import (
    ACTIVATED_EVENT_TYPE,
    AutoTopupPolicySerializer,
    AutoTopupPolicyWriteSerializer,
    EsimSerializer,
    LifecycleEventCreateSerializer,
    LifecycleEventSerializer,
    PurchaseTopupSerializer,
    TopupPackageSerializer,
    TopupSerializer,
    UsageSerializer,
)
from apps.esims.services.auto_topup_service import AutoTopupService
from apps.esims.services.lifecycle_service import lifecycle_service
from apps.esims.services.topup_service import TopupService
from apps.esims.services.usage_service import UsageService
from apps.orders.exceptions import (
    IdempotencyKeyRequiredError,
    ProviderFulfillmentError,
    SpendInProgressError,
)
from apps.organizations.services import (
    require_assign_esim,
    require_spend,
    require_view,
    resolve_account_context,
)
from apps.organizations.services.context import AccountContext
from apps.pricing.presentation import pricing_account_for_request
from core.openapi_serializers import (
    ErrorDetailSerializer,
    InsufficientCreditsSerializer,
)
from shared.providers.factory import get_topup_provider

ORGANIZATION_CONTEXT_PARAMETER = OpenApiParameter(
    name="organization_id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.QUERY,
    required=False,
    description=(
        "Team Account context via organization id. Omit for personal Account. "
        "Authorization uses ``Esim.account`` only — never client ``account_id`` "
        "or ``Esim.user``."
    ),
)


def _truthy_query_flag(raw: str | None) -> bool:
    """Parse a boolean query flag; missing/empty → False."""
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class OwnedEsimMixin:
    """Scopes eSIM lookups to the resolved Account (``Esim.account`` only).

    Optional query ``organization_id`` selects team context (ADR 020).
    ``Esim.user`` / ``assigned_user`` are never used for authz.
    """

    def resolve_inventory_context(self) -> AccountContext:
        cached = getattr(self, "_account_context", None)
        if cached is not None:
            return cached
        organization_id = self.request.query_params.get("organization_id") or None
        if organization_id == "":
            organization_id = None
        context = resolve_account_context(
            self.request.user, organization_id=organization_id
        )
        if context.kind == "organization":
            require_view(context)
        self._account_context = context
        return context

    def require_inventory_mutation(self) -> AccountContext:
        """Gate presentation mutations (note / archive / lifecycle events)."""
        context = self.resolve_inventory_context()
        if context.kind == "organization":
            require_assign_esim(context)
        return context

    def require_spend_mutation(self) -> AccountContext:
        """Gate team spend mutations (auto-topup policy write/delete)."""
        context = self.resolve_inventory_context()
        if context.kind == "organization":
            require_spend(context)
        return context

    def get_queryset(self):
        context = self.resolve_inventory_context()
        return (
            Esim.objects.filter(account=context.account)
            .select_related("order", "account")
            .prefetch_related(
                Prefetch(
                    "lifecycle_events",
                    queryset=EsimLifecycleEvent.objects.filter(
                        event_type=ACTIVATED_EVENT_TYPE
                    ).order_by("created_at"),
                    to_attr="_activated_events",
                ),
                Prefetch(
                    "auto_topup_policies",
                    queryset=EsimAutoTopupPolicy.objects.only(
                        "id", "esim_id", "enabled", "status", "reason"
                    ),
                ),
            )
        )


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_list",
        summary="List my eSIMs",
        description=(
            "List eSIMs owned by the resolved Account (``Esim.account``). "
            "Omit ``organization_id`` for personal Account; set it for team "
            "inventory (requires ``can_view``). By default archived eSIMs are "
            "omitted (``include_archived=false``). ``archived_at`` is "
            "presentation-only and does not affect lifecycle, top-up, billing, "
            "or provider sync."
        ),
        parameters=[
            ORGANIZATION_CONTEXT_PARAMETER,
            OpenApiParameter(
                name="include_archived",
                type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
                required=False,
                default=False,
                description=(
                    "When false (default), exclude rows with ``archived_at`` "
                    "set. When true, include archived eSIMs in the page."
                ),
                examples=[
                    OpenApiExample(
                        "Default — hide archived",
                        value=False,
                        description="GET /api/v1/me/esims/?include_archived=false",
                    ),
                    OpenApiExample(
                        "Include archived",
                        value=True,
                        description="GET /api/v1/me/esims/?include_archived=true",
                    ),
                ],
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=EsimSerializer(many=True), description="Paginated eSIMs"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active / cannot view",
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Organization not found"
            ),
        },
    ),
)
class EsimListView(OwnedEsimMixin, ListAPIView):
    """List eSIMs for the resolved Account context."""

    serializer_class = EsimSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        if not _truthy_query_flag(self.request.query_params.get("include_archived")):
            queryset = queryset.filter(archived_at__isnull=True)
        return queryset


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_retrieve",
        summary="Retrieve my eSIM",
        description=(
            "Retrieve a single eSIM owned by the resolved Account "
            "(``Esim.account``)."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            200: OpenApiResponse(response=EsimSerializer, description="eSIM"),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active / cannot view",
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
            "``note`` is local metadata and is never synchronized to Airalo. "
            "``archived_at`` is not writable here — use POST archive/unarchive. "
            "Team context requires ``can_assign_esim`` and an active Organization. "
            "PUT is not supported."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        request=EsimSerializer,
        responses={
            200: OpenApiResponse(response=EsimSerializer, description="eSIM"),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not allowed"
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

    def partial_update(self, request: Request, *args, **kwargs) -> Response:
        self.require_inventory_mutation()
        return super().partial_update(request, *args, **kwargs)


@extend_schema_view(
    post=extend_schema(
        tags=["eSIM"],
        operation_id="esim_archive",
        summary="Archive my eSIM",
        description=(
            "Set ``archived_at`` on an owned eSIM (presentation visibility). "
            "Idempotent: if already archived, returns 200 with the current "
            "resource. Concurrent requests are safe; last committed write wins. "
            "Does not change lifecycle ``status``, top-up eligibility, or "
            "provider state. Team context requires ``can_assign_esim`` and an "
            "active Organization."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        request=None,
        responses={
            200: OpenApiResponse(
                response=EsimSerializer,
                description="eSIM (archived_at set)",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not allowed"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
)
class EsimArchiveView(OwnedEsimMixin, GenericAPIView):
    """Archive an owned eSIM (presentation-only ``archived_at``)."""

    serializer_class = EsimSerializer
    http_method_names = ["post", "head", "options"]

    def post(self, request: Request, *args, **kwargs) -> Response:
        self.require_inventory_mutation()
        esim = self.get_object()
        if esim.archived_at is None:
            esim.archived_at = timezone.now()
            esim.save(update_fields=["archived_at", "updated_at"])
        return Response(self.get_serializer(esim).data)


@extend_schema_view(
    post=extend_schema(
        tags=["eSIM"],
        operation_id="esim_unarchive",
        summary="Unarchive my eSIM",
        description=(
            "Clear ``archived_at`` on an owned eSIM (presentation visibility). "
            "Idempotent: if not archived, returns 200 with the current "
            "resource. Team context requires ``can_assign_esim`` and an active "
            "Organization."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        request=None,
        responses={
            200: OpenApiResponse(
                response=EsimSerializer,
                description="eSIM (archived_at cleared)",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not allowed"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM not found"
            ),
        },
    ),
)
class EsimUnarchiveView(OwnedEsimMixin, GenericAPIView):
    """Unarchive an owned eSIM (clear presentation-only ``archived_at``)."""

    serializer_class = EsimSerializer
    http_method_names = ["post", "head", "options"]

    def post(self, request: Request, *args, **kwargs) -> Response:
        self.require_inventory_mutation()
        esim = self.get_object()
        if esim.archived_at is not None:
            esim.archived_at = None
            esim.save(update_fields=["archived_at", "updated_at"])
        return Response(self.get_serializer(esim).data)


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_usage",
        summary="eSIM usage",
        description=(
            "Fetch live usage for an eSIM owned by the resolved Account and "
            "refresh the cache."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            200: OpenApiResponse(
                response=UsageSerializer, description="Usage snapshot"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active / cannot view",
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
        description=(
            "Chronological install / lifecycle trail for an eSIM owned by the "
            "resolved Account."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            200: OpenApiResponse(
                response=LifecycleEventSerializer(many=True),
                description="Lifecycle events",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active / cannot view",
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
            "``idempotency_key`` per eSIM. Team context requires "
            "``can_assign_esim`` and an active Organization."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
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
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not allowed"
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
        self.require_inventory_mutation()
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
        description=(
            "List available top-up packages for an eSIM owned by the resolved "
            "Account (``Esim.account``). Omit ``organization_id`` for personal "
            "Account; set the query param for team context. "
            "Additive pricing fields match catalog packages "
            "(``price_usd`` customer charge, ``list_price_usd`` provider list)."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            200: OpenApiResponse(
                response=TopupPackageSerializer(many=True),
                description="Top-up package list",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active",
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
            "Purchase a top-up using prepaid credits on the resolved Account. "
            "Ownership requires ``Esim.account`` to match the context Account "
            "(``Esim.user`` is not authz). Idempotent on ``idempotency_key``. "
            "Omit ``organization_id`` for personal spend; set it for team spend "
            "(requires ``can_spend``). Client ``account_id`` is rejected."
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
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Not allowed to spend in organization context",
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
class EsimTopupsView(GenericAPIView):
    """List or purchase top-ups for an Account-owned eSIM (ADR 020)."""

    serializer_class = PurchaseTopupSerializer
    queryset = Esim.objects.all()

    def _esim_for_context(self, context: AccountContext) -> Esim:
        """Load eSIM only when ``Esim.account`` matches resolved Account."""
        try:
            return Esim.objects.select_related("account", "order").get(
                pk=self.kwargs["pk"],
                account=context.account,
            )
        except Esim.DoesNotExist as exc:
            raise NotFound(detail="Not found.") from exc

    def get(self, request: Request, *args, **kwargs) -> Response:
        organization_id = request.query_params.get("organization_id") or None
        context = resolve_account_context(request.user, organization_id=organization_id)
        esim = self._esim_for_context(context)
        packages = TopupService(get_topup_provider()).list_topups(esim)
        serializer = TopupPackageSerializer(
            packages,
            many=True,
            context={
                "request": request,
                "pricing_account": pricing_account_for_request(request),
            },
        )
        return Response({"results": serializer.data})

    def post(self, request: Request, *args, **kwargs) -> Response:
        if not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")

        serializer = PurchaseTopupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        organization_id = serializer.validated_data.get("organization_id")
        context = resolve_account_context(request.user, organization_id=organization_id)
        if context.kind == "organization":
            require_spend(context)

        esim = self._esim_for_context(context)

        service = TopupService(get_topup_provider())
        try:
            topup = service.purchase(
                esim,
                package_id=serializer.validated_data["package_id"],
                idempotency_key=serializer.validated_data["idempotency_key"],
                account=context.account,
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


def _parse_if_match_version(header: str | None) -> int | None:
    """Parse ``If-Match`` as an integer policy version (optional quotes / W/)."""
    if header is None or not str(header).strip():
        return None
    value = str(header).strip()
    if value == "*":
        return None
    if value.upper().startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("If-Match must be an integer version") from exc


def _resolve_expected_version(
    *,
    body_version: int | None,
    if_match: str | None,
) -> int | None:
    header_version = _parse_if_match_version(if_match)
    if header_version is not None and body_version is not None:
        if header_version != body_version:
            raise ValueError("If-Match and body version disagree")
        return header_version
    if header_version is not None:
        return header_version
    return body_version


@extend_schema_view(
    get=extend_schema(
        tags=["eSIM"],
        operation_id="esim_auto_topup_retrieve",
        summary="Get auto top-up policy",
        description=(
            "Return the auto top-up policy for an eSIM owned by the resolved "
            "Account, if any."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            200: OpenApiResponse(
                response=AutoTopupPolicySerializer, description="Policy"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Membership not active / cannot view",
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM or policy not found"
            ),
        },
    ),
    put=extend_schema(
        tags=["eSIM"],
        operation_id="esim_auto_topup_upsert",
        summary="Create or update auto top-up policy",
        description=(
            "Upsert auto top-up policy (v2: ``expiry_enabled`` + ``usage_mode``). "
            "``package_id`` must be in Available top-ups. Optimistic concurrency via "
            "body ``version`` and/or ``If-Match`` header (required when updating). "
            "Policy is bound to the resolved Account. Team context requires "
            "``can_spend`` and an active Organization. Returns 409 if a purchase "
            "for this eSIM is still in progress."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        request=AutoTopupPolicyWriteSerializer,
        responses={
            200: OpenApiResponse(
                response=AutoTopupPolicySerializer, description="Policy updated"
            ),
            201: OpenApiResponse(
                response=AutoTopupPolicySerializer, description="Policy created"
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Invalid request"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Rollout gate denied or spend not allowed",
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="eSIM/package not found or auto top-up disabled",
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Version conflict or spend in progress",
            ),
        },
    ),
    delete=extend_schema(
        tags=["eSIM"],
        operation_id="esim_auto_topup_destroy",
        summary="Delete auto top-up policy",
        description=(
            "Delete the auto top-up policy. Requires matching ``If-Match`` version "
            "(or ``version`` query param). Team context requires ``can_spend`` and "
            "an active Organization."
        ),
        parameters=[ORGANIZATION_CONTEXT_PARAMETER],
        responses={
            204: OpenApiResponse(description="Policy deleted"),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not allowed"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="eSIM or policy not found"
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer, description="Version conflict"
            ),
        },
    ),
)
class EsimAutoTopupView(OwnedEsimMixin, GenericAPIView):
    """GET/PUT/DELETE auto top-up policy for an Account-owned eSIM."""

    serializer_class = AutoTopupPolicyWriteSerializer

    def get(self, request: Request, *args, **kwargs) -> Response:
        esim = self.get_object()
        policy = EsimAutoTopupPolicy.objects.filter(esim=esim).first()
        if policy is None:
            raise NotFound(detail="Not found.")
        return Response(AutoTopupPolicySerializer(policy).data)

    def put(self, request: Request, *args, **kwargs) -> Response:
        if not settings.AUTO_TOPUP_ENABLED or not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")

        context = self.require_spend_mutation()
        esim = self.get_object()
        serializer = AutoTopupPolicyWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            expected = _resolve_expected_version(
                body_version=data.get("version"),
                if_match=request.headers.get("If-Match"),
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        existed = EsimAutoTopupPolicy.objects.filter(esim=esim).exists()
        if existed and expected is None:
            return Response(
                {
                    "detail": "version or If-Match is required when updating a policy",
                    "code": "VERSION_REQUIRED",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = AutoTopupService(get_topup_provider())
        try:
            policy = service.upsert_policy(
                esim=esim,
                account=context.account,
                package_id=data["package_id"],
                expiry_enabled=data["expiry_enabled"],
                usage_mode=data["usage_mode"],
                renew_mode=data["renew_mode"],
                threshold_mb=data.get("threshold_mb"),
                remaining_count=data.get("remaining_count"),
                active_until=data.get("active_until"),
                enabled=data.get("enabled", True),
                expected_version=expected,
                actor=f"user:{request.user.pk}",
            )
        except PermissionError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_403_FORBIDDEN,
            )
        except TopupPackageNotFoundError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_404_NOT_FOUND,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except SpendInProgressError as exc:
            return Response(
                {
                    "detail": str(exc),
                    "code": "SPEND_IN_PROGRESS",
                },
                status=status.HTTP_409_CONFLICT,
            )
        except LookupError:
            return Response(
                {"detail": "Policy version conflict", "code": "VERSION_CONFLICT"},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            AutoTopupPolicySerializer(policy).data,
            status=status.HTTP_200_OK if existed else status.HTTP_201_CREATED,
        )

    def delete(self, request: Request, *args, **kwargs) -> Response:
        if not settings.AUTO_TOPUP_ENABLED or not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")

        self.require_spend_mutation()
        esim = self.get_object()
        version_raw = request.query_params.get("version")
        body_version: int | None = None
        if version_raw is not None and str(version_raw).strip() != "":
            try:
                body_version = int(version_raw)
            except ValueError:
                return Response(
                    {"detail": "version must be an integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        try:
            expected = _resolve_expected_version(
                body_version=body_version,
                if_match=request.headers.get("If-Match"),
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if expected is None:
            return Response(
                {
                    "detail": "version query param or If-Match is required",
                    "code": "VERSION_REQUIRED",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = AutoTopupService(get_topup_provider())
        try:
            service.delete_policy(
                esim=esim,
                expected_version=expected,
                actor=f"user:{request.user.pk}",
            )
        except EsimAutoTopupPolicy.DoesNotExist as exc:
            raise NotFound(detail="Not found.") from exc
        except LookupError:
            return Response(
                {"detail": "Policy version conflict", "code": "VERSION_CONFLICT"},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
