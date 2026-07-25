"""Billing HTTP API — only under ``/api/v1/billing/`` (ADR-010)."""

from __future__ import annotations

from django.conf import settings
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
)
from apps.billing.models import DepositRequest
from apps.billing.serializers import (
    BalanceSerializer,
    BillingConfigSerializer,
    DepositInfoSerializer,
    DepositRequestSerializer,
    VerifyDepositSerializer,
)
from apps.billing.services.account import ensure_billing_account
from apps.billing.services.deposit_info import (
    BILLING_CONFIG_CACHE_MAX_AGE,
    billing_config_etag,
    get_billing_config,
    get_deposit_info,
)
from apps.billing.services.deposit_verification import deposit_verification_service
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


class BillingConfigView(APIView):
    """GET /api/v1/billing/config/ — public display config (AllowAny; no secrets)."""

    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        payload = BillingConfigSerializer(get_billing_config()).data
        response = Response(payload)
        response["Cache-Control"] = f"public, max-age={BILLING_CONFIG_CACHE_MAX_AGE}"
        response["ETag"] = billing_config_etag(payload)
        return response


class BalanceView(BillingAPIView):
    """GET /api/v1/billing/balance/"""

    def get(self, request: Request) -> Response:
        account = ensure_billing_account(request.user)
        return Response(BalanceSerializer({"balance": account.balance}).data)


class DepositInfoView(BillingAPIView):
    """GET /api/v1/billing/deposit-info/ — full Polygon USDT meta + EIP-681 URI."""

    def get(self, request: Request) -> Response:
        return Response(DepositInfoSerializer(get_deposit_info()).data)


class VerifyWalletDepositView(BillingAPIView):
    """POST /api/v1/billing/verify-wallet/"""

    require_walletconnect = True

    def post(self, request: Request) -> Response:
        return _verify_deposit(
            request,
            payment_method=DepositRequest.PaymentMethod.WALLET_CONNECT,
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
