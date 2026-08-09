"""Organization read + invite API (ADR 020).

Authz uses ``organization_id`` path/context only. Client-supplied ``account_id``
is never used as an authorization source.
"""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.organizations.exceptions import (
    DeviceBindingConflictError,
    DeviceBindingNotFoundError,
    IccidNotFoundError,
    InviteConflictError,
    InviteInvalidError,
    LastOwnerError,
    NotAllowedError,
    UemInventoryUnavailableError,
)
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    Organization,
)
from apps.organizations.permissions import permissions_for_role
from apps.organizations.serializers import (
    DeviceBindingCreateSerializer,
    DeviceBindingCredentialResponseSerializer,
    DeviceBindingSerializer,
    DeviceCoverageSerializer,
    DeviceStatusRequestSerializer,
    DeviceStatusSerializer,
    MembershipRoleUpdateSerializer,
    MembershipSerializer,
    OrganizationCreateSerializer,
    OrganizationInviteAcceptResponseSerializer,
    OrganizationInviteAcceptSerializer,
    OrganizationInviteCreateResponseSerializer,
    OrganizationInviteCreateSerializer,
    OrganizationInviteSerializer,
    OrganizationSerializer,
    OrganizationTransferOwnershipResponseSerializer,
    OrganizationTransferOwnershipSerializer,
)
from apps.organizations.services.account_binding import create_organization
from apps.organizations.services.authz import require_view
from apps.organizations.services.context import resolve_organization_context
from apps.organizations.services.device_binding import (
    create_device_binding,
    get_device_binding,
    list_device_bindings,
    rotate_device_credential,
    unbind_device_binding,
)
from apps.organizations.services.device_status import (
    get_device_coverage_by_credential,
    get_device_status,
    get_device_status_by_credential,
)
from apps.organizations.services.invites import (
    accept_invite,
    create_invite,
    list_pending_invites,
    revoke_invite,
)
from apps.organizations.services.membership import (
    revoke_membership,
    set_member_role,
    transfer_ownership,
)
from apps.organizations.throttles import (
    DeviceCoverageRateThrottle,
    DeviceStatusRateThrottle,
)
from core.openapi_serializers import ErrorDetailSerializer


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Conflict."
    default_code = "conflict"


def _map_invite_error(exc: Exception) -> None:
    """Convert invite domain errors to DRF exceptions (never returns)."""
    if isinstance(exc, InviteConflictError):
        raise Conflict(detail=str(exc)) from exc
    if isinstance(exc, InviteInvalidError):
        raise ValidationError({"detail": str(exc)}) from exc
    if isinstance(exc, NotAllowedError):
        raise PermissionDenied(detail=str(exc)) from exc
    raise exc


def _map_membership_error(exc: Exception) -> None:
    """Convert membership domain errors to DRF exceptions (never returns)."""
    if isinstance(exc, LastOwnerError):
        raise PermissionDenied(detail=str(exc)) from exc
    if isinstance(exc, NotAllowedError):
        raise PermissionDenied(detail=str(exc)) from exc
    raise exc


def _map_device_binding_error(exc: Exception) -> None:
    """Convert device-binding domain errors to DRF exceptions (never returns)."""
    if isinstance(exc, DeviceBindingConflictError):
        raise Conflict(detail=str(exc)) from exc
    if isinstance(exc, DeviceBindingNotFoundError):
        raise NotFound(detail=str(exc)) from exc
    if isinstance(exc, NotAllowedError):
        raise PermissionDenied(detail=str(exc)) from exc
    raise exc


def _membership_for_org(*, organization_id, membership_id) -> Membership:
    membership = (
        Membership.objects.select_related("user", "organization")
        .filter(pk=membership_id, organization_id=organization_id)
        .first()
    )
    if membership is None:
        raise NotFound(detail="Not found.")
    return membership


class OrganizationsAPIView(APIView):
    """JWT-authenticated org endpoint; ``ORGANIZATIONS_ENABLED=false`` → 404."""

    permission_classes = [IsAuthenticated]

    def initial(self, request: Request, *args, **kwargs) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.ORGANIZATIONS_ENABLED:
            raise NotFound(detail="Not found.")


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_list",
        summary="List my organizations",
        description=(
            "Organizations where the caller has an **active** membership. "
            "Team Account is exposed as ``account_id`` for display only — "
            "authorization always uses ``organization_id``."
        ),
        responses={
            200: OpenApiResponse(response=OrganizationSerializer(many=True)),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Organizations feature disabled",
            ),
        },
    ),
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_create",
        summary="Create organization",
        description=(
            "Creates an Organization with a new empty team Account and an "
            "active **owner** membership for the caller. Does not convert or "
            "merge the caller's personal Account or move eSIM inventory."
        ),
        request=OrganizationCreateSerializer,
        responses={
            201: OpenApiResponse(response=OrganizationSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Organizations feature disabled",
            ),
        },
    ),
)
class OrganizationListView(OrganizationsAPIView, ListAPIView):
    serializer_class = OrganizationSerializer
    pagination_class = None

    def get_queryset(self):
        return (
            Organization.objects.filter(
                memberships__user=self.request.user,
                memberships__status=MembershipStatus.ACTIVE,
            )
            .select_related("account")
            .distinct()
            .order_by("name")
        )

    def list(self, request: Request, *args, **kwargs):
        orgs = list(self.get_queryset())
        role_by_org = {
            m.organization_id: m.role
            for m in Membership.objects.filter(
                user=request.user,
                status=MembershipStatus.ACTIVE,
                organization_id__in=[o.pk for o in orgs],
            )
        }
        data = [
            OrganizationSerializer(
                org,
                context={"my_role": role_by_org[org.pk]},
            ).data
            for org in orgs
        ]
        return Response(data)

    def post(self, request: Request, *args, **kwargs) -> Response:
        body = OrganizationCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        org = create_organization(
            name=body.validated_data["name"],
            actor=request.user,
        )
        return Response(
            OrganizationSerializer(
                org,
                context={
                    "my_role": MembershipRole.OWNER,
                    "permissions": permissions_for_role(MembershipRole.OWNER),
                },
            ).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_retrieve",
        summary="Get organization",
        description=(
            "Requires an **active** membership. Unknown or inaccessible "
            "``organization_id`` returns 404. Suspended/revoked membership → 403."
        ),
        responses={
            200: OpenApiResponse(response=OrganizationSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDetailView(OrganizationsAPIView, RetrieveAPIView):
    serializer_class = OrganizationSerializer
    lookup_url_kwarg = "organization_id"

    def get_object(self):
        ctx = resolve_organization_context(
            self.request.user,
            self.kwargs["organization_id"],
        )
        assert ctx.organization is not None
        self._org_context = ctx
        return ctx.organization

    def retrieve(self, request: Request, *args, **kwargs):
        org = self.get_object()
        ctx = self._org_context
        return Response(
            OrganizationSerializer(
                org,
                context={
                    "my_role": ctx.role,
                    "permissions": ctx.permissions,
                },
            ).data
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_transfer_ownership",
        summary="Transfer organization ownership",
        description=(
            "Requires ``can_transfer_ownership`` (owner) on an **active** "
            "organization. The new owner must already be an active member. "
            "The previous owner is demoted to admin. Business rules live in "
            "``transfer_ownership``."
        ),
        request=OrganizationTransferOwnershipSerializer,
        responses={
            200: OpenApiResponse(
                response=OrganizationTransferOwnershipResponseSerializer
            ),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationTransferOwnershipView(OrganizationsAPIView):
    def post(self, request: Request, organization_id) -> Response:
        body = OrganizationTransferOwnershipSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            new_owner = User.objects.get(pk=body.validated_data["new_owner_user_id"])
        except User.DoesNotExist as exc:
            raise NotFound(detail="Not found.") from exc

        try:
            new_owner_membership = transfer_ownership(
                actor=request.user,
                organization_id=organization_id,
                new_owner=new_owner,
            )
        except (LastOwnerError, NotAllowedError) as exc:
            _map_membership_error(exc)
            raise

        # Caller's role/permissions after transfer (typically admin).
        ctx = resolve_organization_context(request.user, organization_id)
        assert ctx.organization is not None
        new_owner_membership = Membership.objects.select_related("user").get(
            pk=new_owner_membership.pk
        )
        return Response(
            OrganizationTransferOwnershipResponseSerializer(
                {
                    "organization": ctx.organization,
                    "new_owner_membership": new_owner_membership,
                },
                context={
                    "my_role": ctx.role,
                    "permissions": ctx.permissions,
                },
            ).data
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_members_list",
        summary="List organization members",
        description=(
            "Requires an **active** membership with ``can_view``. "
            "Does not accept ``account_id`` as an authorization input."
        ),
        responses={
            200: OpenApiResponse(response=MembershipSerializer(many=True)),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationMembersListView(OrganizationsAPIView, ListAPIView):
    serializer_class = MembershipSerializer
    pagination_class = None

    def list(self, request: Request, *args, **kwargs):
        ctx = resolve_organization_context(
            request.user,
            kwargs["organization_id"],
        )
        require_view(ctx)
        assert ctx.organization is not None
        qs = (
            Membership.objects.filter(organization=ctx.organization)
            .select_related("user")
            .order_by("created_at")
        )
        return Response(MembershipSerializer(qs, many=True).data)


@extend_schema_view(
    patch=extend_schema(
        tags=["Organizations"],
        operation_id="organization_members_update_role",
        summary="Update member role",
        description=(
            "Requires ``can_manage_members`` on an **active** organization. "
            "Cannot assign ``owner`` (use transfer ownership) and cannot change "
            "the current owner's role. Business rules live in "
            "``set_member_role``."
        ),
        request=MembershipRoleUpdateSerializer,
        responses={
            200: OpenApiResponse(response=MembershipSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationMemberDetailView(OrganizationsAPIView):
    def patch(self, request: Request, organization_id, membership_id) -> Response:
        body = MembershipRoleUpdateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        membership = _membership_for_org(
            organization_id=organization_id,
            membership_id=membership_id,
        )
        try:
            updated = set_member_role(
                actor=request.user,
                organization_id=organization_id,
                target_user=membership.user,
                role=body.validated_data["role"],
            )
        except (LastOwnerError, NotAllowedError) as exc:
            _map_membership_error(exc)
            raise
        return Response(MembershipSerializer(updated).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_members_revoke",
        summary="Revoke membership",
        description=(
            "Requires ``can_manage_members``. Cannot revoke the sole active "
            "owner. Idempotent when already revoked. Business rules live in "
            "``revoke_membership``."
        ),
        request=None,
        responses={
            200: OpenApiResponse(response=MembershipSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationMemberRevokeView(OrganizationsAPIView):
    def post(self, request: Request, organization_id, membership_id) -> Response:
        membership = _membership_for_org(
            organization_id=organization_id,
            membership_id=membership_id,
        )
        try:
            updated = revoke_membership(
                actor=request.user,
                organization_id=organization_id,
                target_user=membership.user,
            )
        except (LastOwnerError, NotAllowedError) as exc:
            _map_membership_error(exc)
            raise
        return Response(MembershipSerializer(updated).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_invites_list",
        summary="List pending organization invites",
        description=(
            "Requires ``can_invite`` (owner/admin) on an **active** organization. "
            "Does not accept ``account_id`` as an authorization input."
        ),
        responses={
            200: OpenApiResponse(response=OrganizationInviteSerializer(many=True)),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_invites_create",
        summary="Create or refresh a pending invite",
        description=(
            "Creates a pending invite, or refreshes the existing pending invite "
            "for the same normalized email (token rotated). Owner role is not "
            "inviteable. Email delivery is out of scope for this endpoint."
        ),
        request=OrganizationInviteCreateSerializer,
        responses={
            200: OpenApiResponse(
                response=OrganizationInviteCreateResponseSerializer,
                description="Existing pending invite refreshed.",
            ),
            201: OpenApiResponse(
                response=OrganizationInviteCreateResponseSerializer,
                description="New pending invite created.",
            ),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
            409: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationInviteListCreateView(OrganizationsAPIView):
    def get(self, request: Request, organization_id) -> Response:
        try:
            qs = list_pending_invites(
                actor=request.user,
                organization_id=organization_id,
            )
        except (InviteConflictError, InviteInvalidError, NotAllowedError) as exc:
            _map_invite_error(exc)
            raise
        return Response(
            OrganizationInviteSerializer(
                qs.select_related("invited_by"),
                many=True,
            ).data
        )

    def post(self, request: Request, organization_id) -> Response:
        body = OrganizationInviteCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = create_invite(
                actor=request.user,
                organization_id=organization_id,
                email=body.validated_data["email"],
                role=body.validated_data["role"],
            )
        except (InviteConflictError, InviteInvalidError, NotAllowedError) as exc:
            _map_invite_error(exc)
            raise
        payload = OrganizationInviteCreateResponseSerializer(
            {
                "invite": result.invite,
                "token": result.raw_token,
                "created": result.created,
            }
        ).data
        return Response(
            payload,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_invites_revoke",
        summary="Revoke a pending invite",
        description="Requires ``can_invite``. Idempotent for already-revoked invites.",
        request=None,
        responses={
            200: OpenApiResponse(response=OrganizationInviteSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationInviteRevokeView(OrganizationsAPIView):
    def post(self, request: Request, organization_id, invite_id) -> Response:
        try:
            invite = revoke_invite(
                actor=request.user,
                organization_id=organization_id,
                invite_id=invite_id,
            )
        except (InviteConflictError, InviteInvalidError, NotAllowedError) as exc:
            _map_invite_error(exc)
            raise
        return Response(
            OrganizationInviteSerializer(invite).data,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_invites_accept",
        summary="Accept an organization invite",
        description=(
            "Accept by single-use token. Email on the authenticated user must match "
            "the invite. Does not merge wallets or move eSIM inventory. Concurrent "
            "accepts are serialized; the second call is idempotent for the same user."
        ),
        request=OrganizationInviteAcceptSerializer,
        responses={
            200: OpenApiResponse(response=OrganizationInviteAcceptResponseSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationInviteAcceptView(OrganizationsAPIView):
    def post(self, request: Request) -> Response:
        body = OrganizationInviteAcceptSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = accept_invite(
                actor=request.user,
                raw_token=body.validated_data["token"],
            )
        except (InviteConflictError, InviteInvalidError, NotAllowedError) as exc:
            _map_invite_error(exc)
            raise
        return Response(
            OrganizationInviteAcceptResponseSerializer(
                {
                    "membership": result.membership,
                    "organization_id": result.invite.organization_id,
                    "already_accepted": result.already_accepted,
                }
            ).data
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_bindings_list",
        summary="List device bindings",
        description=(
            "List DeviceBinding rows for the organization. Requires ``can_view``. "
            "Bindings attach team Account eSIMs to RoamKit-issued "
            "``device_external_id`` values (ADR 020). No UEM sync in this API."
        ),
        responses={
            200: OpenApiResponse(response=DeviceBindingSerializer(many=True)),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_bindings_create",
        summary="Create device binding",
        description=(
            "Bind a team Account eSIM to a new RoamKit-issued "
            "``device_external_id``. Requires ``can_device_bind`` and an active "
            "Organization. Personal-Account eSIMs are rejected (404). "
            "One active binding per eSIM (use ``replace=true`` to rebind). "
            "Client ``account_id`` / ``device_external_id`` are rejected."
        ),
        request=DeviceBindingCreateSerializer,
        responses={
            201: OpenApiResponse(response=DeviceBindingCredentialResponseSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
            409: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDeviceBindingListCreateView(OrganizationsAPIView):
    def get(self, request: Request, organization_id) -> Response:
        bindings = list_device_bindings(request.user, organization_id)
        return Response(DeviceBindingSerializer(bindings, many=True).data)

    def post(self, request: Request, organization_id) -> Response:
        body = DeviceBindingCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = create_device_binding(
                request.user,
                organization_id,
                esim_id=body.validated_data["esim_id"],
                replace=body.validated_data.get("replace", False),
            )
        except (DeviceBindingConflictError, DeviceBindingNotFoundError) as exc:
            _map_device_binding_error(exc)
            raise
        return Response(
            DeviceBindingCredentialResponseSerializer(
                {"binding": result.binding, "credential": result.credential}
            ).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_bindings_retrieve",
        summary="Retrieve device binding",
        description="Get one DeviceBinding in organization scope (``can_view``).",
        responses={
            200: OpenApiResponse(response=DeviceBindingSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDeviceBindingDetailView(OrganizationsAPIView):
    def get(self, request: Request, organization_id, binding_id) -> Response:
        binding = get_device_binding(request.user, organization_id, binding_id)
        return Response(DeviceBindingSerializer(binding).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_bindings_unbind",
        summary="Unbind device binding",
        description=(
            "Mark an active DeviceBinding as ``unbound`` (soft lifecycle). "
            "Requires ``can_device_bind`` and an active Organization."
        ),
        request=None,
        responses={
            200: OpenApiResponse(response=DeviceBindingSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDeviceBindingUnbindView(OrganizationsAPIView):
    def post(self, request: Request, organization_id, binding_id) -> Response:
        try:
            binding = unbind_device_binding(request.user, organization_id, binding_id)
        except (DeviceBindingConflictError, DeviceBindingNotFoundError) as exc:
            _map_device_binding_error(exc)
            raise
        return Response(DeviceBindingSerializer(binding).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_bindings_rotate_credential",
        summary="Rotate device credential",
        description=(
            "Issue a new opaque device credential for an active binding. "
            "The previous secret stops working immediately. Plaintext is returned "
            "only in this response. Requires ``can_device_bind``."
        ),
        request=None,
        responses={
            200: OpenApiResponse(response=DeviceBindingCredentialResponseSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDeviceBindingRotateCredentialView(OrganizationsAPIView):
    def post(self, request: Request, organization_id, binding_id) -> Response:
        try:
            result = rotate_device_credential(request.user, organization_id, binding_id)
        except (DeviceBindingConflictError, DeviceBindingNotFoundError) as exc:
            _map_device_binding_error(exc)
            raise
        return Response(
            DeviceBindingCredentialResponseSerializer(
                {"binding": result.binding, "credential": result.credential}
            ).data
        )


@extend_schema_view(
    get=extend_schema(
        tags=["Organizations"],
        operation_id="organization_device_status",
        summary="Device status snapshot",
        description=(
            "Read-only UEM status for an **active** DeviceBinding looked up by "
            "``device_external_id`` within ``organization_id``. Requires "
            "``can_view``. ``device_external_id`` is a lookup key only — never a "
            "credential. Usage comes from the eSIM cache (no provider refresh). "
            "Unbound/replaced/cross-org bindings return 404. No BlackBerry sync."
        ),
        responses={
            200: OpenApiResponse(response=DeviceStatusSerializer),
            401: OpenApiResponse(response=ErrorDetailSerializer),
            403: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
        },
    ),
)
class OrganizationDeviceStatusView(OrganizationsAPIView):
    def get(
        self, request: Request, organization_id, device_external_id: str
    ) -> Response:
        snapshot = get_device_status(
            request.user,
            organization_id,
            device_external_id=device_external_id,
        )
        return Response(DeviceStatusSerializer(snapshot.as_response_dict()).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Device"],
        operation_id="device_status",
        summary="Device-facing status snapshot",
        description=(
            "Read-only status for managed devices. Authenticate with "
            "``device_external_id`` + opaque ``credential`` in the body "
            "(never put the secret in the URL). Same snapshot shape as the "
            "org status API. Unbound/replaced/wrong credential → 404. "
            "No user JWT; rate-limited by IP."
        ),
        request=DeviceStatusRequestSerializer,
        responses={
            200: OpenApiResponse(response=DeviceStatusSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
            429: OpenApiResponse(response=ErrorDetailSerializer),
            503: OpenApiResponse(response=ErrorDetailSerializer),
        },
        auth=[],
    ),
)
class DeviceStatusView(APIView):
    """Unauthenticated device status via opaque credential (PR18).

    Optional staging path (ADR 021 override): when the binding has
    ``uem_device_guid``, resolve ICCID via read-only UEM and look up Esim on
    the team Account. Classic PR18 ``binding.esim`` path when guid is empty.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [DeviceStatusRateThrottle]

    def initial(self, request: Request, *args, **kwargs) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.ORGANIZATIONS_ENABLED:
            raise NotFound(detail="Not found.")

    def post(self, request: Request) -> Response:
        body = DeviceStatusRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            snapshot = get_device_status_by_credential(
                device_external_id=body.validated_data["device_external_id"],
                credential=body.validated_data["credential"],
            )
        except UemInventoryUnavailableError as exc:
            return Response(
                {
                    "detail": str(exc) or "UEM telephony inventory unavailable.",
                    "code": "uem_inventory_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except IccidNotFoundError as exc:
            return Response(
                {
                    "detail": str(exc) or "No RoamKit data for this ICCID.",
                    "code": "iccid_not_found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(DeviceStatusSerializer(snapshot.as_response_dict()).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Device"],
        operation_id="device_coverage",
        summary="Device-facing coverage snapshot",
        description=(
            "Read-only purchase-time coverage list for managed devices. "
            "Authenticate with ``device_external_id`` + opaque ``credential`` "
            "in the body (same ownership boundary as device status). "
            "Never accepts ``esim_id``. Legacy orders without a coverage "
            "snapshot return ``coverage: null``. No user JWT; rate-limited "
            "by IP."
        ),
        request=DeviceStatusRequestSerializer,
        responses={
            200: OpenApiResponse(response=DeviceCoverageSerializer),
            400: OpenApiResponse(response=ErrorDetailSerializer),
            404: OpenApiResponse(response=ErrorDetailSerializer),
            429: OpenApiResponse(response=ErrorDetailSerializer),
            503: OpenApiResponse(response=ErrorDetailSerializer),
        },
        auth=[],
    ),
)
class DeviceCoverageView(APIView):
    """Unauthenticated device coverage via opaque credential."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [DeviceCoverageRateThrottle]

    def initial(self, request: Request, *args, **kwargs) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.ORGANIZATIONS_ENABLED:
            raise NotFound(detail="Not found.")

    def post(self, request: Request) -> Response:
        body = DeviceStatusRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            snapshot = get_device_coverage_by_credential(
                device_external_id=body.validated_data["device_external_id"],
                credential=body.validated_data["credential"],
            )
        except UemInventoryUnavailableError as exc:
            return Response(
                {
                    "detail": str(exc) or "UEM telephony inventory unavailable.",
                    "code": "uem_inventory_unavailable",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except IccidNotFoundError as exc:
            return Response(
                {
                    "detail": str(exc) or "No RoamKit data for this ICCID.",
                    "code": "iccid_not_found",
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(DeviceCoverageSerializer(snapshot.as_response_dict()).data)
