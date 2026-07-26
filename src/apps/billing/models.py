"""Billing models — prepaid credit ledger, deposits, and subscriptions."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db import models


class AppendOnlyViolation(Exception):
    """Raised when an append-only model is updated or deleted."""


class LedgerReferenceType(models.TextChoices):
    DEPOSIT = "deposit", "Deposit"
    ORDER = "order", "Order"
    TOPUP = "topup", "Top-up"
    SUBSCRIPTION = "subscription", "Subscription"
    REFUND = "refund", "Refund"
    ADMIN_ADJUSTMENT = "admin_adjustment", "Admin adjustment"


class Account(models.Model):
    """Billing account 1:1 with User. ``balance`` is a cache of the ledger."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_account",
    )
    balance = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        default=Decimal("0"),
    )
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "account"
        verbose_name_plural = "accounts"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(balance__gte=0),
                name="billing_account_balance_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=0),
                name="billing_account_version_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Account {self.pk} ({self.user})"


class DepositRequest(models.Model):
    """A USDT deposit awaiting or completed on-chain verification."""

    class PaymentMethod(models.TextChoices):
        WALLET_CONNECT = "wallet_connect", "WalletConnect"
        CEX_MANUAL = "cex_manual", "CEX manual"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="deposit_requests",
    )
    amount_requested = models.DecimalField(max_digits=20, decimal_places=6)
    amount_credited = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    payment_method = models.CharField(
        max_length=32,
        choices=PaymentMethod.choices,
        help_text="How the USDT transfer was initiated (WalletConnect or CEX/manual).",
    )
    tx_hash = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=128, unique=True, db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
        help_text="Deposit verification status (pending / completed / failed).",
    )
    failure_reason = models.TextField(blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    raw_rpc_response = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "deposit request"
        verbose_name_plural = "deposit requests"
        indexes = [
            models.Index(fields=["account", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount_requested__gt=0),
                name="billing_deposit_amount_requested_gt_0",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(amount_credited__isnull=True)
                    | models.Q(amount_credited__gt=0)
                ),
                name="billing_deposit_amount_credited_null_or_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Deposit {self.pk} ({self.status})"


class CreditLedgerEntry(models.Model):
    """Append-only ledger entry. Source of truth for account balances."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="ledger_entries",
    )
    delta = models.DecimalField(max_digits=20, decimal_places=6)
    balance_after = models.DecimalField(max_digits=20, decimal_places=6)
    reference_type = models.CharField(
        max_length=32,
        choices=LedgerReferenceType.choices,
        db_index=True,
        help_text="Domain object that caused this ledger entry (deposit, order, …).",
    )
    reference_id = models.CharField(max_length=64, db_index=True)
    idempotency_key = models.CharField(max_length=128, unique=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "credit ledger entry"
        verbose_name_plural = "credit ledger entries"
        indexes = [
            models.Index(fields=["account", "created_at"]),
            models.Index(fields=["reference_type", "reference_id"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(delta=0),
                name="billing_ledger_delta_ne_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Ledger {self.pk} ({self.delta})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyViolation(
                "CreditLedgerEntry is append-only; updates are forbidden"
            )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation(
            "CreditLedgerEntry is append-only; deletes are forbidden"
        )


class Subscription(models.Model):
    """Recurring eSIM renewal billed against prepaid credits."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        EXPIRED = "expired", "Expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        Account,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    esim = models.ForeignKey(
        "esims.Esim",
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    price_per_period = models.DecimalField(max_digits=20, decimal_places=6)
    next_billing_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "subscription"
        verbose_name_plural = "subscriptions"
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["status", "next_billing_date"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price_per_period__gt=0),
                name="billing_subscription_price_per_period_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Subscription {self.pk} ({self.status})"


# Admin deep-link registry (ORDER / TOPUP filled in AppConfig.ready).
REFERENCE_MODELS: dict[str, type[models.Model] | None] = {
    LedgerReferenceType.DEPOSIT: DepositRequest,
    LedgerReferenceType.ORDER: None,  # resolved to orders.Order in BillingConfig.ready
    LedgerReferenceType.TOPUP: None,  # resolved to esims.Topup in EsimsConfig.ready
    LedgerReferenceType.SUBSCRIPTION: Subscription,
    LedgerReferenceType.REFUND: None,
    LedgerReferenceType.ADMIN_ADJUSTMENT: None,
}
