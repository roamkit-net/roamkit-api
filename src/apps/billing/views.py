"""Billing HTTP API — only under ``/api/v1/billing/`` (ADR-010)."""

from __future__ import annotations

from django.conf import settings
from django.http import HttpResponse
from django.utils.cache import get_conditional_response
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
)
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.exceptions import (
    BillingDisabledError,
    DepositVerificationError,
    DepositVerificationFailedError,
    DuplicateTransactionError,
    InsufficientConfirmationsError,
    InvalidAmountError,
    VoucherError,
    VouchersDisabledError,
)
from apps.billing.models import DepositRequest
from apps.billing.serializers import (
    BalanceSerializer,
    BillingConfigSerializer,
    DepositInfoSerializer,
    DepositRequestSerializer,
    VerifyDepositSerializer,
    VoucherErrorSerializer,
    VoucherRedeemRequestSerializer,
    VoucherRedeemResponseSerializer,
)
from apps.billing.services.account import ensure_billing_account
from apps.billing.services.deposit_info import (
    BILLING_CONFIG_CACHE_MAX_AGE,
    billing_config_etag,
    get_billing_config,
    get_deposit_info,
)
from apps.billing.services.deposit_verification import deposit_verification_service
from apps.billing.services.voucher_redeem import voucher_redeem_service
from apps.billing.throttles import BillingVoucherRedeemRateThrottle
from core.http.client_ip import get_client_ip
from core.http.request_id import get_or_create_request_id
from core.openapi_serializers import ErrorDetailSerializer
from shared.providers.blockchain import BlockchainRPCError


class BillingAPIView(APIView):
    """JWT-authenticated billing endpoint; ``BILLING_ENABLED=false`` → 404."""

    permission_classes = [IsAuthenticated]
    require_walletconnect = False

    def initial(self, request: Request, *args, **kwargs) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.BILLING_ENABLED:
            raise NotFound(detail="Not found.")
        if self.require_walletconnect and not settings.WALLETCONNECT_ENABLED:
            raise PermissionDenied(detail="WalletConnect deposits are disabled.")


@extend_schema_view(
    get=extend_schema(
        tags=["Billing"],
        operation_id="billing_config",
        summary="Billing display config",
        description=(
            "Public currency / presentation config for catalog prices. Supports ETag / "
            "If-None-Match (304). No secrets."
        ),
        auth=[],
        responses={
            200: OpenApiResponse(
                response=BillingConfigSerializer,
                description="Display currency config",
                examples=[
                    OpenApiExample(
                        "USDT display config",
                        value={
                            "config_version": 1,
                            "token_symbol": "USDT",
                            "token_name": "Tether USD",
                            "token_decimals": 6,
                            "display_decimals": 2,
                            "billing_enabled": True,
                        },
                    )
                ],
            ),
            304: OpenApiResponse(description="Not modified (ETag match)"),
        },
    ),
)
class BillingConfigView(APIView):
    """GET /api/v1/billing/config/ — public display config (AllowAny; no secrets)."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> HttpResponse:
        payload = BillingConfigSerializer(get_billing_config()).data
        etag = billing_config_etag(payload)
        response = Response(payload)
        response["Cache-Control"] = f"public, max-age={BILLING_CONFIG_CACHE_MAX_AGE}"
        response["ETag"] = etag
        # Matching If-None-Match → 304, empty body, Cache-Control retained.
        return get_conditional_response(request, etag=etag, response=response)


@extend_schema_view(
    get=extend_schema(
        tags=["Billing"],
        operation_id="billing_balance",
        summary="Account credit balance",
        description="Return the authenticated user's prepaid credit balance.",
        responses={
            200: OpenApiResponse(
                response=BalanceSerializer,
                description="Balance",
                examples=[
                    OpenApiExample("Sample balance", value={"balance": "42.500000"})
                ],
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Billing disabled"
            ),
        },
    ),
)
class BalanceView(BillingAPIView):
    """GET /api/v1/billing/balance/"""

    def get(self, request: Request) -> Response:
        account = ensure_billing_account(request.user)
        return Response(BalanceSerializer({"balance": account.balance}).data)


@extend_schema_view(
    get=extend_schema(
        tags=["Billing"],
        operation_id="billing_deposit_info",
        summary="Deposit metadata",
        description=(
            "Polygon USDT deposit metadata including EIP-681 URI "
            "for the platform wallet."
        ),
        responses={
            200: OpenApiResponse(
                response=DepositInfoSerializer, description="Deposit info"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Billing disabled"
            ),
        },
    ),
)
class DepositInfoView(BillingAPIView):
    """GET /api/v1/billing/deposit-info/ — full Polygon USDT meta + EIP-681 URI."""

    def get(self, request: Request) -> Response:
        return Response(DepositInfoSerializer(get_deposit_info()).data)


@extend_schema_view(
    post=extend_schema(
        tags=["Billing"],
        operation_id="billing_verify_wallet",
        summary="Verify WalletConnect deposit",
        description=(
            "Verify an on-chain USDT transfer initiated via WalletConnect. "
            "Requires ``WALLETCONNECT_ENABLED``."
        ),
        request=VerifyDepositSerializer,
        responses={
            200: OpenApiResponse(
                response=DepositRequestSerializer, description="Verified deposit"
            ),
            202: OpenApiResponse(
                response=DepositRequestSerializer,
                description="Accepted; waiting for confirmations",
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Verification failed"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            403: OpenApiResponse(
                response=ErrorDetailSerializer, description="WalletConnect disabled"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Billing disabled"
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer, description="Duplicate transaction"
            ),
            502: OpenApiResponse(
                response=ErrorDetailSerializer, description="Blockchain RPC error"
            ),
        },
    ),
)
class VerifyWalletDepositView(BillingAPIView):
    """POST /api/v1/billing/verify-wallet/"""

    require_walletconnect = True

    def post(self, request: Request) -> Response:
        return _verify_deposit(
            request,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
        )


@extend_schema_view(
    post=extend_schema(
        tags=["Billing"],
        operation_id="billing_verify_cex",
        summary="Verify CEX / manual deposit",
        description=(
            "Verify an on-chain USDT transfer credited from a CEX or manual send."
        ),
        request=VerifyDepositSerializer,
        responses={
            200: OpenApiResponse(
                response=DepositRequestSerializer, description="Verified deposit"
            ),
            202: OpenApiResponse(
                response=DepositRequestSerializer,
                description="Accepted; waiting for confirmations",
            ),
            400: OpenApiResponse(
                response=ErrorDetailSerializer, description="Verification failed"
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer, description="Billing disabled"
            ),
            409: OpenApiResponse(
                response=ErrorDetailSerializer, description="Duplicate transaction"
            ),
            502: OpenApiResponse(
                response=ErrorDetailSerializer, description="Blockchain RPC error"
            ),
        },
    ),
)
class VerifyCexDepositView(BillingAPIView):
    """POST /api/v1/billing/verify-cex/"""

    def post(self, request: Request) -> Response:
        return _verify_deposit(
            request,
            payment_method=DepositRequest.PaymentMethod.CEX_MANUAL,
        )


def _verify_deposit(request: Request, *, payment_method: str) -> Response:
    serializer = VerifyDepositSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    account = ensure_billing_account(request.user)
    idempotency_key = data["idempotency_key"]

    try:
        deposit = deposit_verification_service.verify(
            account,
            tx_hash=data["tx_hash"],
            payment_method=payment_method,
            amount_requested=data["amount_requested"],
            idempotency_key=idempotency_key,
        )
    except BillingDisabledError as exc:
        raise NotFound(detail="Not found.") from exc
    except InsufficientConfirmationsError as exc:
        deposit = DepositRequest.objects.filter(idempotency_key=idempotency_key).first()
        if deposit is None:
            return Response({"detail": str(exc)}, status=status.HTTP_202_ACCEPTED)
        payload = {
            **DepositRequestSerializer(deposit).data,
            "confirmations": exc.confirmations,
            "required_confirmations": exc.required,
        }
        return Response(payload, status=status.HTTP_202_ACCEPTED)
    except DuplicateTransactionError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
    except (DepositVerificationFailedError, InvalidAmountError) as exc:
        deposit = DepositRequest.objects.filter(idempotency_key=idempotency_key).first()
        if deposit is not None:
            return Response(
                DepositRequestSerializer(deposit).data,
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except DepositVerificationError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except BlockchainRPCError as exc:
        deposit = DepositRequest.objects.filter(idempotency_key=idempotency_key).first()
        if deposit is not None:
            return Response(
                DepositRequestSerializer(deposit).data,
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    return Response(DepositRequestSerializer(deposit).data, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        tags=["Billing", "Vouchers"],
        operation_id="billing_vouchers_redeem",
        summary="Redeem voucher or campaign code",
        description=(
            "Credit the authenticated billing account from a UNIQUE voucher or "
            "SHARED campaign code. Money path: VoucherRedeemService → CreditService "
            "(ADR 011). Requires ``BILLING_ENABLED`` and ``VOUCHERS_ENABLED``."
        ),
        request=VoucherRedeemRequestSerializer,
        responses={
            200: OpenApiResponse(
                response=VoucherRedeemResponseSerializer,
                description="Credits applied",
                examples=[
                    OpenApiExample(
                        "Success",
                        value={"credited": "25.000000", "balance": "120.500000"},
                    )
                ],
            ),
            400: OpenApiResponse(
                response=VoucherErrorSerializer,
                description="Invalid / expired / revoked / reserved / limit",
                examples=[
                    OpenApiExample(
                        "Invalid",
                        value={
                            "code": "voucher_invalid",
                            "detail": "Invalid voucher code",
                        },
                    ),
                    OpenApiExample(
                        "Expired",
                        value={
                            "code": "voucher_expired",
                            "detail": "Voucher expired",
                        },
                    ),
                    OpenApiExample(
                        "Revoked",
                        value={
                            "code": "voucher_revoked",
                            "detail": "Voucher revoked",
                        },
                    ),
                ],
            ),
            401: OpenApiResponse(
                response=ErrorDetailSerializer, description="Authentication required"
            ),
            404: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Billing or vouchers disabled",
                examples=[
                    OpenApiExample(
                        "Disabled",
                        value={"detail": "Not found."},
                    )
                ],
            ),
            429: OpenApiResponse(
                response=ErrorDetailSerializer,
                description="Rate limited (10 attempts / 5 minutes per user)",
            ),
        },
    ),
)
class VoucherRedeemView(BillingAPIView):
    """POST /api/v1/billing/vouchers/redeem/"""

    throttle_classes = [BillingVoucherRedeemRateThrottle]

    def initial(self, request: Request, *args, **kwargs) -> None:
        super().initial(request, *args, **kwargs)
        if not settings.VOUCHERS_ENABLED:
            raise NotFound(detail="Not found.")

    def post(self, request: Request) -> Response:
        serializer = VoucherRedeemRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = ensure_billing_account(request.user)
        request_id = get_or_create_request_id(request)
        try:
            result = voucher_redeem_service.redeem(
                account=account,
                code=serializer.validated_data["code"],
                request_id=request_id,
                client_ip=get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:512],
            )
        except VouchersDisabledError as exc:
            raise NotFound(detail="Not found.") from exc
        except VoucherError as exc:
            return Response(exc.to_api_dict(), status=status.HTTP_400_BAD_REQUEST)

        return Response(
            VoucherRedeemResponseSerializer(
                {"credited": result.credited, "balance": result.balance}
            ).data,
            status=status.HTTP_200_OK,
        )
