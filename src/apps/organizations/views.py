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
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.exceptions import (
    InviteConflictError,
    InviteInvalidError,
    LastOwnerError,
    NotAllowedError,
)
from apps.organizations.models import (
    Membership,
    MembershipRole,
    MembershipStatus,
    Organization,
)
from apps.organizations.permissions import permissions_for_role
from apps.organizations.serializers import (
    MembershipRoleUpdateSerializer,
    MembershipSerializer,
    OrganizationCreateSerializer,
    OrganizationInviteAcceptResponseSerializer,
    OrganizationInviteAcceptSerializer,
    OrganizationInviteCreateResponseSerializer,
    OrganizationInviteCreateSerializer,
    OrganizationInviteSerializer,
    OrganizationSerializer,
)
from apps.organizations.services.account_binding import create_organization
from apps.organizations.services.authz import require_view
from apps.organizations.services.context import resolve_organization_context
from apps.organizations.services.invites import (
    accept_invite,
    create_invite,
    list_pending_invites,
    revoke_invite,
)
from apps.organizations.services.membership import (
    revoke_membership,
    set_member_role,
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
