"""Path-scoped partner portal routes (ADR 024).

Channel id in the path is identification only. Authorization is
``resolve_authorized_partner_context`` on every channel-scoped request.
"""

from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    ParseError,
    UnsupportedMediaType,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.billing.exceptions import InsufficientFundsError, InvalidAmountError
from apps.billing.partner_channel import PartnerCreditGrant, PartnerInviteLink
from apps.billing.serializers import (
    PartnerChannelCodeSerializer,
    PartnerContextListSerializer,
    PartnerCustomersPageSerializer,
    PartnerCustomersQueryError,
    PartnerGrantBodyError,
    PartnerGrantRequestSerializer,
    PartnerGrantResponseSerializer,
    PartnerGrantsPageSerializer,
    PartnerGrantsQueryError,
    PartnerInviteLinkSerializer,
    PartnerSummarySerializer,
    parse_partner_customers_query,
    parse_partner_grant_body,
    parse_partner_grants_query,
)
from apps.billing.services.partner_context import (
    PartnerAccessDenied,
    list_authorized_partner_contexts,
    partner_role_can_grant,
    partner_role_can_manage_invite,
    resolve_authorized_partner_context,
)
from apps.billing.services.partner_customers import partner_customers_service
from apps.billing.services.partner_grant import (
    CustomerAttributionChanged,
    CustomerNotAttributed,
    PartnerChannelDisabled,
    PartnerGrantError,
    PartnerGrantForbidden,
    PartnerGrantIdempotencyConflict,
    PartnerGrantNegativeBalance,
    PartnerGrantSameAccount,
    partner_grant_service,
)
from apps.billing.services.partner_grants import partner_grants_service
from apps.billing.services.partner_invite import (
    invite_link_for,
    regenerate_invite_link,
    set_invite_active,
)
from apps.billing.services.partner_log import log_partner_event
from apps.billing.services.partner_settlement import (
    PartnerChannelOwnershipInvalid,
    PartnerSettlementAccountMissing,
    resolve_partner_settlement_account,
)
from apps.billing.services.partner_summary import (
    PartnerSummary,
    partner_summary_service,
)
from apps.billing.throttles import (
    PartnerGrantRateThrottle,
    PartnerInviteMutationThrottle,
)
from core.http.request_id import get_or_create_request_id

logger = logging.getLogger(__name__)

_NO_STORE = "no-store"
_SETTLEMENT_CODE = "partner_settlement_account_missing"


def _coded(http_status: int, code: str) -> Response:
    response = Response({"code": code}, status=http_status)
    response["Cache-Control"] = _NO_STORE
    return response


def _summary_body(summary: PartnerSummary) -> dict:
    counts = summary.accrual_counts
    return {
        "total_earned": summary.total_earned,
        "available_balance": summary.available_balance,
        "accrual_counts": {
            "order": counts.order,
            "topup": counts.topup,
            "subscription": counts.subscription,
            "total": counts.total,
        },
    }


class _PartnerReadView(APIView):
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return _coded(status.HTTP_401_UNAUTHORIZED, "authentication_required")
        return super().handle_exception(exc)


@extend_schema_view(
    get=extend_schema(
        tags=["Partner"],
        operation_id="partner_contexts_list",
        summary="List authorized partner contexts",
        description=(
            "Every partner channel this user may access. An empty list is a "
            "normal answer. is_active is accrual display status and does not "
            "remove a context. capabilities are role checks only: they ignore "
            "is_active, the feature flag, attribution, and balance. The "
            "response has no account, balance, organization, or user ids."
        ),
        responses={
            200: OpenApiResponse(
                response=PartnerContextListSerializer,
                description="Authorized contexts, possibly empty",
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled",
            ),
        },
    ),
)
class PartnerContextListView(_PartnerReadView):
    """Discover contexts. This response does not select one channel."""

    def get(self, request: Request) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        contexts = [
            {
                "channel_id": item.channel_id,
                "kind": item.kind,
                "label": item.label,
                "effective_role": item.effective_role,
                "is_active": item.channel.is_active,
                "capabilities": {
                    "can_grant": partner_role_can_grant(item.effective_role),
                    "can_manage_invite": partner_role_can_manage_invite(
                        item.effective_role
                    ),
                },
            }
            for item in list_authorized_partner_contexts(request.user)
        ]
        response = Response(
            PartnerContextListSerializer({"contexts": contexts}).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        log_partner_event(
            "partner.context.list",
            user_id=request.user.pk,
            context_count=len(contexts),
        )
        return response


@extend_schema_view(
    get=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_summary",
        summary="Summary for one partner channel",
        description=(
            "Stored margin total and display balance for the channel in the "
            "path. The channel id is not authority. A missing channel and an "
            "inaccessible channel are the same partner_access_denied response. "
            "There is no fallback to another context."
        ),
        responses={
            200: OpenApiResponse(
                response=PartnerSummarySerializer,
                description="Channel summary",
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_access_denied",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled",
            ),
            500: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_settlement_account_missing",
            ),
        },
    ),
)
class PartnerChannelSummaryView(_PartnerReadView):
    """Read one requested channel. Viewer, admin, and owner may read."""

    def get(self, request: Request, channel_id) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            context = resolve_authorized_partner_context(request.user, channel_id)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        try:
            account = resolve_partner_settlement_account(context.channel)
        except (PartnerSettlementAccountMissing, PartnerChannelOwnershipInvalid) as exc:
            logger.exception(
                "partner_channel_summary.settlement_invalid partner_channel_id=%s "
                "error_type=%s",
                context.channel_id,
                type(exc).__name__,
            )
            return _coded(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                _SETTLEMENT_CODE,
            )
        summary = partner_summary_service.summarize_for_account(
            context.channel,
            account,
        )
        response = Response(
            PartnerSummarySerializer(_summary_body(summary)).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        response["X-Partner-Role"] = context.effective_role
        return response


_CUSTOMER_PARAMETERS = [
    OpenApiParameter("page", OpenApiTypes.INT, required=False),
    OpenApiParameter("page_size", OpenApiTypes.INT, required=False),
    OpenApiParameter(
        "sort",
        OpenApiTypes.STR,
        required=False,
        enum=["attributed_at", "total_partner_earned", "accrual_count"],
    ),
    OpenApiParameter(
        "order",
        OpenApiTypes.STR,
        required=False,
        enum=["asc", "desc"],
    ),
    OpenApiParameter("q", OpenApiTypes.STR, required=False),
]


@extend_schema_view(
    get=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_customers",
        summary="Customers for one partner channel",
        description=(
            "Current attributions for the channel in the path, with earnings "
            "from stored accruals on that same channel. The channel id is not "
            "authority. A missing channel and an inaccessible channel are the "
            "same partner_access_denied response. Channel is_active does not "
            "hide customers. There is no fallback to another context."
        ),
        parameters=_CUSTOMER_PARAMETERS,
        responses={
            200: OpenApiResponse(
                response=PartnerCustomersPageSerializer,
                description="Customer page",
            ),
            400: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description=(
                    "invalid_page, invalid_page_size, invalid_sort, "
                    "invalid_order, or invalid_query"
                ),
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_access_denied",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled",
            ),
        },
    ),
)
class PartnerChannelCustomersView(_PartnerReadView):
    """List current customers of one requested channel."""

    def get(self, request: Request, channel_id) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            context = resolve_authorized_partner_context(request.user, channel_id)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        try:
            query = parse_partner_customers_query(request.query_params)
        except PartnerCustomersQueryError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)
        page = partner_customers_service.list_customers(context.channel, query)
        response = Response(
            PartnerCustomersPageSerializer(
                {
                    "count": page.count,
                    "page": page.page,
                    "page_size": page.page_size,
                    "results": [
                        {
                            "customer_id": row.customer_id,
                            "email": row.email,
                            "display_name": row.display_name,
                            "attributed_at": row.attributed_at,
                            "total_partner_earned": row.total_partner_earned,
                            "accrual_count": row.accrual_count,
                        }
                        for row in page.results
                    ],
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        response["X-Partner-Role"] = context.effective_role
        return response


def _grant_exists(channel_id: object, idempotency_key: str) -> bool:
    return PartnerCreditGrant.objects.filter(
        partner_channel_id=channel_id,
        idempotency_key=idempotency_key,
    ).exists()


_GRANT_LIST_PARAMETERS = [
    OpenApiParameter("page", OpenApiTypes.INT, required=False),
    OpenApiParameter("page_size", OpenApiTypes.INT, required=False),
    OpenApiParameter(
        "sort",
        OpenApiTypes.STR,
        required=False,
        enum=["created_at", "amount"],
    ),
    OpenApiParameter(
        "order",
        OpenApiTypes.STR,
        required=False,
        enum=["asc", "desc"],
    ),
]


@extend_schema_view(
    get=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_grants",
        summary="Grant history for one partner channel",
        description=(
            "Stored credit grants for the channel in the path. The channel id "
            "is not authority. A missing channel and an inaccessible channel "
            "are the same partner_access_denied response. There is no fallback."
        ),
        parameters=_GRANT_LIST_PARAMETERS,
        responses={
            200: OpenApiResponse(
                response=PartnerGrantsPageSerializer,
                description="Grant page",
            ),
            400: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description=(
                    "invalid_page, invalid_page_size, invalid_sort, or invalid_order"
                ),
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_access_denied",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled",
            ),
        },
    ),
    post=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_grant",
        summary="Grant credits on one partner channel",
        description=(
            "Move credits from this channel's settlement account to a customer "
            "who is currently attributed to this same channel. The path channel "
            "id is the only partner selector. The body is customer_id, amount, "
            "and idempotency_key. An individual grant may spend the whole "
            "available personal balance. A missing channel and an inaccessible "
            "channel are the same partner_access_denied response. There is no "
            "fallback to another context."
        ),
        request=PartnerGrantRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=PartnerGrantResponseSerializer,
                description="Created grant, or the original grant on replay",
            ),
            400: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="invalid_request or invalid_amount",
            ),
            401: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="authentication_required",
            ),
            403: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_access_denied or partner_grant_forbidden",
            ),
            404: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_channel_disabled or customer_not_found",
            ),
            409: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description=(
                    "insufficient_funds, customer_attribution_changed, "
                    "idempotency_key_conflict, or partner_grant_same_account"
                ),
            ),
            429: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="rate_limited",
            ),
            500: OpenApiResponse(
                response=PartnerChannelCodeSerializer,
                description="partner_settlement_account_missing",
            ),
        },
    ),
)
class PartnerChannelGrantView(_PartnerReadView):
    """Grant on the path channel. Viewer may read history but cannot grant."""

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, UnsupportedMediaType):
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")
        return super().handle_exception(exc)

    def get(self, request: Request, channel_id) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            context = resolve_authorized_partner_context(request.user, channel_id)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        try:
            query = parse_partner_grants_query(request.query_params)
        except PartnerGrantsQueryError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)
        page = partner_grants_service.list_grants(context.channel, query)
        response = Response(
            PartnerGrantsPageSerializer(
                {
                    "count": page.count,
                    "page": page.page,
                    "page_size": page.page_size,
                    "results": [
                        {
                            "grant_id": row.grant_id,
                            "customer_id": row.customer_id,
                            "email": row.email,
                            "display_name": row.display_name,
                            "amount": row.amount,
                            "granted_by": (
                                None
                                if row.granted_by is None
                                else {
                                    "user_id": row.granted_by.user_id,
                                    "email": row.granted_by.email,
                                    "display_name": row.granted_by.display_name,
                                }
                            ),
                            "created_at": row.created_at,
                        }
                        for row in page.results
                    ],
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        response["X-Partner-Role"] = context.effective_role
        return response

    def post(self, request: Request, channel_id) -> Response:
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            context = resolve_authorized_partner_context(request.user, channel_id)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")
        if not partner_role_can_grant(context.effective_role):
            log_partner_event(
                "partner.grant.forbidden",
                user_id=request.user.pk,
                partner_channel_id=context.channel.pk,
                kind=context.channel.kind,
                effective_role=context.effective_role,
            )
            return _coded(status.HTTP_403_FORBIDDEN, "partner_grant_forbidden")

        try:
            payload = request.data
        except ParseError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")
        try:
            parsed = parse_partner_grant_body(payload)
        except PartnerGrantBodyError as exc:
            return _coded(status.HTTP_400_BAD_REQUEST, exc.code)

        customer = User.objects.filter(pk=parsed.customer_id).first()
        if customer is None:
            return _coded(status.HTTP_404_NOT_FOUND, "customer_not_found")

        idempotency_key = parsed.idempotency_key
        if not _grant_exists(context.channel_id, idempotency_key):
            self.partner_channel_id = context.channel_id
            if not PartnerGrantRateThrottle().allow_request(request, self):
                return _coded(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")

        try:
            account = resolve_partner_settlement_account(context.channel)
        except (PartnerSettlementAccountMissing, PartnerChannelOwnershipInvalid) as exc:
            logger.exception(
                "partner_channel_grant.settlement_invalid partner_channel_id=%s "
                "error_type=%s",
                context.channel_id,
                type(exc).__name__,
            )
            return _coded(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                _SETTLEMENT_CODE,
            )

        try:
            grant = partner_grant_service.grant_from_account(
                actor=request.user,
                partner_channel=context.channel,
                source_account=account,
                customer=customer,
                amount=parsed.amount,
                idempotency_key=idempotency_key,
                request_id=get_or_create_request_id(request),
            )
        except PartnerChannelDisabled:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        except PartnerGrantForbidden:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_grant_forbidden")
        except CustomerNotAttributed:
            return _coded(status.HTTP_404_NOT_FOUND, "customer_not_found")
        except CustomerAttributionChanged:
            log_partner_event(
                "partner.grant.attribution_changed",
                user_id=request.user.pk,
                partner_channel_id=context.channel.pk,
                kind=context.channel.kind,
                customer_user_id=customer.pk,
            )
            return _coded(status.HTTP_409_CONFLICT, "customer_attribution_changed")
        except PartnerGrantIdempotencyConflict:
            return _coded(status.HTTP_409_CONFLICT, "idempotency_key_conflict")
        except PartnerGrantSameAccount:
            log_partner_event(
                "partner.grant.same_account",
                user_id=request.user.pk,
                partner_channel_id=context.channel.pk,
                kind=context.channel.kind,
                customer_user_id=customer.pk,
            )
            return _coded(status.HTTP_409_CONFLICT, "partner_grant_same_account")
        except InsufficientFundsError:
            log_partner_event(
                "partner.grant.insufficient_funds",
                user_id=request.user.pk,
                partner_channel_id=context.channel.pk,
                kind=context.channel.kind,
                customer_user_id=customer.pk,
            )
            return _coded(status.HTTP_409_CONFLICT, "insufficient_funds")
        except PartnerGrantNegativeBalance:
            return _coded(status.HTTP_409_CONFLICT, "insufficient_funds")
        except InvalidAmountError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_amount")
        except PartnerGrantError:
            return _coded(status.HTTP_400_BAD_REQUEST, "invalid_request")

        response = Response(
            PartnerGrantResponseSerializer(
                {
                    "grant_id": grant.pk,
                    "customer_id": grant.customer_user_id_snapshot,
                    "amount": grant.amount,
                    "created_at": grant.created_at,
                }
            ).data,
            status=status.HTTP_200_OK,
        )
        response["Cache-Control"] = _NO_STORE
        return response


def _invite_body(link) -> dict:
    return {
        "url": link.url,
        "is_active": link.is_active,
        "created_at": link.created_at,
        "regenerated_at": link.regenerated_at,
    }


def _invite_response(link, context) -> Response:
    response = Response(
        PartnerInviteLinkSerializer(_invite_body(link)).data,
        status=status.HTTP_200_OK,
    )
    response["Cache-Control"] = _NO_STORE
    response["X-Partner-Role"] = context.effective_role
    return response


class _ChannelInviteView(_PartnerReadView):
    """Invite link for the path channel. Writes stay owner-only."""

    def _context(self, request: Request, channel_id):
        if not settings.PARTNER_CHANNEL_ENABLED:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_channel_disabled")
        try:
            return resolve_authorized_partner_context(request.user, channel_id)
        except PartnerAccessDenied:
            return _coded(status.HTTP_403_FORBIDDEN, "partner_access_denied")

    def _owner_context(self, request: Request, channel_id):
        context = self._context(request, channel_id)
        if isinstance(context, Response):
            return context
        if not partner_role_can_manage_invite(context.effective_role):
            return _coded(status.HTTP_403_FORBIDDEN, "partner_invite_forbidden")
        self.partner_channel_id = context.channel.pk
        if not PartnerInviteMutationThrottle().allow_request(request, self):
            return _coded(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited")
        return context

    def _link_or_missing(self, channel):
        try:
            return invite_link_for(channel)
        except PartnerInviteLink.DoesNotExist:
            return _coded(status.HTTP_404_NOT_FOUND, "partner_invite_missing")


_INVITE_READ = {
    200: OpenApiResponse(
        response=PartnerInviteLinkSerializer,
        description="Invite link",
    ),
    401: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="authentication_required",
    ),
    403: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_access_denied",
    ),
    404: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_channel_disabled or partner_invite_missing",
    ),
}

_INVITE_WRITE = {
    **_INVITE_READ,
    403: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="partner_access_denied or partner_invite_forbidden",
    ),
    429: OpenApiResponse(
        response=PartnerChannelCodeSerializer,
        description="rate_limited",
    ),
}


@extend_schema_view(
    get=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_invite_link",
        summary="Invite link for one partner channel",
        description=(
            "Canonical invite link for the channel in the path. Viewer may "
            "read. The channel id is not authority."
        ),
        responses=_INVITE_READ,
    ),
)
class PartnerChannelInviteLinkView(_ChannelInviteView):
    def get(self, request: Request, channel_id) -> Response:
        context = self._context(request, channel_id)
        if isinstance(context, Response):
            return context
        link = self._link_or_missing(context.channel)
        if isinstance(link, Response):
            return link
        return _invite_response(link, context)


@extend_schema_view(
    post=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_invite_link_regenerate",
        summary="Regenerate one channel invite link",
        request=None,
        responses=_INVITE_WRITE,
    ),
)
class PartnerChannelInviteRegenerateView(_ChannelInviteView):
    def post(self, request: Request, channel_id) -> Response:
        context = self._owner_context(request, channel_id)
        if isinstance(context, Response):
            return context
        link = regenerate_invite_link(
            context.channel,
            actor=request.user,
            request_id=get_or_create_request_id(request),
        )
        return _invite_response(link, context)


@extend_schema_view(
    post=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_invite_link_activate",
        summary="Activate one channel invite link",
        request=None,
        responses=_INVITE_WRITE,
    ),
)
class PartnerChannelInviteActivateView(_ChannelInviteView):
    def post(self, request: Request, channel_id) -> Response:
        context = self._owner_context(request, channel_id)
        if isinstance(context, Response):
            return context
        link = set_invite_active(
            context.channel,
            actor=request.user,
            active=True,
            request_id=get_or_create_request_id(request),
        )
        return _invite_response(link, context)


@extend_schema_view(
    post=extend_schema(
        tags=["Partner"],
        operation_id="partner_channel_invite_link_deactivate",
        summary="Deactivate one channel invite link",
        request=None,
        responses=_INVITE_WRITE,
    ),
)
class PartnerChannelInviteDeactivateView(_ChannelInviteView):
    def post(self, request: Request, channel_id) -> Response:
        context = self._owner_context(request, channel_id)
        if isinstance(context, Response):
            return context
        link = set_invite_active(
            context.channel,
            actor=request.user,
            active=False,
            request_id=get_or_create_request_id(request),
        )
        return _invite_response(link, context)
