"""Billing API serializers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from rest_framework import serializers

from apps.billing.models import DepositRequest

_BIGINT_MAX = 2**63 - 1
_GRANT_FIELDS = frozenset({"customer_id", "amount", "idempotency_key"})
_AMOUNT_RE = re.compile(r"^(?:0|[1-9]\d{0,13})(?:\.\d{1,6})?$")
_MONEY_QUANT = Decimal("0.000001")


class BalanceSerializer(serializers.Serializer):
    balance = serializers.DecimalField(max_digits=20, decimal_places=6)


class BillingConfigSerializer(serializers.Serializer):
    """Public currency / presentation config (ADR-010 ``GET …/billing/config/``)."""

    config_version = serializers.IntegerField()
    token_symbol = serializers.CharField()
    token_name = serializers.CharField()
    token_decimals = serializers.IntegerField()
    display_decimals = serializers.IntegerField()
    billing_enabled = serializers.BooleanField()


class DepositInfoSerializer(serializers.Serializer):
    """Full Polygon USDT deposit metadata for clients (no hardcoded chain/token)."""

    wallet = serializers.CharField()
    chain_id = serializers.IntegerField()
    token_symbol = serializers.CharField()
    token_decimals = serializers.IntegerField()
    contract = serializers.CharField()
    min_confirmations = serializers.IntegerField()
    eip681_uri = serializers.CharField()
    walletconnect_enabled = serializers.BooleanField()
    subscriptions_enabled = serializers.BooleanField()
    vouchers_enabled = serializers.BooleanField()


class VerifyDepositSerializer(serializers.Serializer):
    tx_hash = serializers.CharField(max_length=128)
    amount_requested = serializers.DecimalField(max_digits=20, decimal_places=6)
    idempotency_key = serializers.CharField(max_length=128)


class DepositRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = DepositRequest
        fields = (
            "id",
            "amount_requested",
            "amount_credited",
            "payment_method",
            "tx_hash",
            "idempotency_key",
            "status",
            "failure_reason",
            "verified_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class VoucherRedeemRequestSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=64, trim_whitespace=False)


class VoucherRedeemResponseSerializer(serializers.Serializer):
    credited = serializers.DecimalField(max_digits=20, decimal_places=6)
    balance = serializers.DecimalField(max_digits=20, decimal_places=6)
    replay = serializers.BooleanField(default=False)


class VoucherErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()


class PartnerGrantBodyError(Exception):
    """Grant body failed the locked envelope. ``code`` is the HTTP code string."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PartnerGrantBody:
    customer_id: int
    amount: Decimal
    idempotency_key: str


def parse_partner_grant_body(data: object) -> PartnerGrantBody:
    """Accept only the three grant fields. Unknown keys are ``invalid_request``.

    ``invalid_amount`` is returned only when every other field is acceptable
    and ``amount`` itself is not a positive ``Decimal(20,6)`` string.
    """
    if not isinstance(data, dict):
        raise PartnerGrantBodyError("invalid_request")

    other_invalid = bool(set(data) - _GRANT_FIELDS or _GRANT_FIELDS - set(data))
    customer_id = data.get("customer_id", None)
    idempotency_key = data.get("idempotency_key", None)
    if (
        isinstance(customer_id, bool)
        or not isinstance(customer_id, int)
        or not 1 <= customer_id <= _BIGINT_MAX
    ):
        other_invalid = True
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        other_invalid = True
    if other_invalid:
        raise PartnerGrantBodyError("invalid_request")

    amount = _parse_grant_amount(data.get("amount"))
    if amount is None:
        raise PartnerGrantBodyError("invalid_amount")
    assert isinstance(customer_id, int)
    assert isinstance(idempotency_key, str)
    return PartnerGrantBody(
        customer_id=customer_id,
        amount=amount,
        idempotency_key=idempotency_key,
    )


def _parse_grant_amount(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or _AMOUNT_RE.fullmatch(raw) is None:
        return None
    value = Decimal(raw)
    if not value.is_finite() or value <= 0:
        return None
    return value.quantize(_MONEY_QUANT)


class PartnerGrantRequestSerializer(serializers.Serializer):
    """Locked grant body: ``customer_id``, ``amount``, ``idempotency_key``."""

    customer_id = serializers.IntegerField(min_value=1, max_value=_BIGINT_MAX)
    amount = serializers.CharField()
    idempotency_key = serializers.CharField(
        min_length=1, max_length=128, trim_whitespace=False
    )

    def to_internal_value(self, data: object) -> dict[str, object]:
        parsed = parse_partner_grant_body(data)
        return {
            "customer_id": parsed.customer_id,
            "amount": parsed.amount,
            "idempotency_key": parsed.idempotency_key,
        }


class PartnerGrantResponseSerializer(serializers.Serializer):
    grant_id = serializers.UUIDField()
    customer_id = serializers.IntegerField(min_value=1, max_value=_BIGINT_MAX)
    amount = serializers.DecimalField(max_digits=20, decimal_places=6)
    created_at = serializers.DateTimeField()


class PartnerGrantCodeSerializer(serializers.Serializer):
    """Partner grant error body. Exactly one code, no field names."""

    code = serializers.CharField()
