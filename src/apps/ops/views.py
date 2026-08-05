"""Staff read-only Operations Dashboard views."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.models import DepositRequest
from apps.ops.mixins import NoStoreCacheMixin
from apps.ops.pagination import OpsPageNumberPagination
from apps.ops.permissions import IsStaff
from apps.ops.serializers import (
    OpsDashboardSerializer,
    OpsDepositListItemSerializer,
    OpsOrderListItemSerializer,
    OpsSearchResponseSerializer,
    OpsUserDetailSerializer,
    OpsUserListItemSerializer,
)
from apps.ops.services.dashboard import build_dashboard
from apps.ops.services.members import (
    serialize_user_detail,
    serialize_user_list_item,
    users_queryset,
)
from apps.ops.services.search import search_ops
from apps.orders.models import Order
from core.openapi_serializers import ErrorDetailSerializer

User = get_user_model()


class OpsAPIView(NoStoreCacheMixin, APIView):
    permission_classes = [IsStaff]


class OpsListAPIView(NoStoreCacheMixin, ListAPIView):
    permission_classes = [IsStaff]
    pagination_class = OpsPageNumberPagination


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_dashboard",
        summary="Operations dashboard aggregate",
        description=(
            "Single read-only payload for the ops home page: KPIs, pending work, "
            "financial summary, destinations, packages, alerts, health, and "
            "activity feed. schema_version is stable for additive field changes."
        ),
        responses={
            200: OpenApiResponse(
                response=OpsDashboardSerializer, description="Dashboard"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
        },
    ),
)
class OpsDashboardView(OpsAPIView):
    def get(self, request: Request) -> Response:
        return Response(build_dashboard(), status=status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_search",
        summary="Global ops search",
        description=(
            "Search users, orders, deposits, eSIMs, and vouchers. "
            "Response always includes all group keys (possibly empty)."
        ),
        parameters=[
            OpenApiParameter(
                name="q",
                type=str,
                location=OpenApiParameter.QUERY,
                required=True,
                description="Search query (min 3 chars, or exact UUID)",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpsSearchResponseSerializer, description="Grouped hits"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
        },
    ),
)
class OpsSearchView(OpsAPIView):
    def get(self, request: Request) -> Response:
        q = request.query_params.get("q", "")
        return Response(search_ops(q), status=status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_users_list",
        summary="List members",
        parameters=[
            OpenApiParameter(
                name="q",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by email (icontains)",
            ),
            OpenApiParameter(
                name="is_active",
                type=bool,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpsUserListItemSerializer(many=True),
                description="Paginated members",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
        },
    ),
)
class OpsUserListView(OpsListAPIView):
    def get_queryset(self):
        qs = users_queryset().order_by("-created_at")
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(email__icontains=q.strip())
        is_active = self.request.query_params.get("is_active")
        if is_active is not None:
            val = is_active.lower() in {"1", "true", "yes"}
            qs = qs.filter(is_active=val)
        return qs

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = [serialize_user_list_item(u) for u in page]
        return self.get_paginated_response(items)


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_users_retrieve",
        summary="Member detail with timeline",
        responses={
            200: OpenApiResponse(
                response=OpsUserDetailSerializer, description="Member detail"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Not found"
            ),
        },
    ),
)
class OpsUserDetailView(OpsAPIView):
    def get(self, request: Request, pk: int) -> Response:
        user = get_object_or_404(users_queryset(), pk=pk)
        return Response(serialize_user_detail(user), status=status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_orders_list",
        summary="List orders",
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpsOrderListItemSerializer(many=True),
                description="Paginated orders",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
        },
    ),
)
class OpsOrderListView(OpsListAPIView):
    def get_queryset(self):
        qs = Order.objects.select_related("account__user").order_by("-created_at")
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = []
        for order in page:
            price = order.retail_price_usd
            items.append(
                {
                    "id": order.pk,
                    "status": order.status,
                    "package_title": order.package_title,
                    "retail_price_usd": f"{price:.2f}" if price is not None else None,
                    "country_code": order.country_code,
                    "user_id": order.account.user_id,
                    "user_email": order.account.user.email,
                    "created_at": order.created_at.isoformat().replace("+00:00", "Z"),
                }
            )
        return self.get_paginated_response(items)


@extend_schema_view(
    get=extend_schema(
        tags=["Ops"],
        operation_id="ops_deposits_list",
        summary="List deposits",
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
            ),
            OpenApiParameter(
                name="q",
                type=str,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Filter by tx_hash or user email",
            ),
        ],
        responses={
            200: OpenApiResponse(
                response=OpsDepositListItemSerializer(many=True),
                description="Paginated deposits",
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="Staff required"
            ),
        },
    ),
)
class OpsDepositListView(OpsListAPIView):
    def get_queryset(self):
        qs = DepositRequest.objects.select_related("account__user").order_by(
            "-created_at"
        )
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        q = self.request.query_params.get("q")
        if q:
            q = q.strip()
            qs = qs.filter(
                Q(tx_hash__icontains=q) | Q(account__user__email__icontains=q)
            )
        return qs

    def list(self, request: Request, *args, **kwargs) -> Response:
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = []
        for deposit in page:
            credited = deposit.amount_credited
            items.append(
                {
                    "id": str(deposit.pk),
                    "status": deposit.status,
                    "amount_requested": f"{deposit.amount_requested:.6f}",
                    "amount_credited": (
                        f"{credited:.6f}" if credited is not None else None
                    ),
                    "payment_method": deposit.payment_method,
                    "tx_hash": deposit.tx_hash or "",
                    "user_id": deposit.account.user_id,
                    "user_email": deposit.account.user.email,
                    "created_at": deposit.created_at.isoformat().replace("+00:00", "Z"),
                }
            )
        return self.get_paginated_response(items)
