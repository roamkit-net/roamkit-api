"""Organization read API (ADR 020 / PR3).

Authz uses ``organization_id`` path/context only. Client-supplied ``account_id``
is never used as an authorization source.
"""

from __future__ import annotations

from django.conf import settings
from drf_spectacular.utils import OpenApiResponse, extend_schema, extend_schema_view
from rest_framework.exceptions import NotFound
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.organizations.models import Membership, MembershipStatus, Organization
from apps.organizations.serializers import MembershipSerializer, OrganizationSerializer
from apps.organizations.services.authz import require_view
from apps.organizations.services.context import resolve_organization_context
from core.openapi_serializers import ErrorDetailSerializer


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
