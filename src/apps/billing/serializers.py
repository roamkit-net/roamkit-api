"""Billing API serializers."""

from __future__ import annotations

from rest_framework import serializers

from apps.billing.models import DepositRequest


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


class VoucherErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    detail = serializers.CharField()
