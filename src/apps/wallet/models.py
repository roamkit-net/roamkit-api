"""Wallet domain models — WalletIdentity, WalletAddress / Index Registry (RFC 003/004).

Credits stay in ``apps.billing``. This app must not mutate ledger balances.
"""

from __future__ import annotations

import uuid

from django.db import models


class WalletChain(models.TextChoices):
    """Supported receive chains. Polygon first (ADR 017 / RFC 004)."""

    POLYGON = "polygon", "Polygon"


class WalletAddressStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RETIRED = "retired", "Retired"


class WalletIdentity(models.Model):
    """Logical funding wallet for one billing Account (RFC 003).

    Provider-agnostic domain object — no seed access required to create.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.OneToOneField(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="wallet_identity",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "wallet identity"
        verbose_name_plural = "wallet identities"

    def __str__(self) -> str:
        return f"WalletIdentity {self.pk} (account={self.account_id})"


class WalletAddress(models.Model):
    """Receive endpoint + Index Registry row (RFC 003 / RFC 004).

    ``derivation_index`` is platform state: immutable and never reused.
    Together with ``address`` and lifecycle, this row *is* the Index Registry entry
    for HD recovery (Seed + Index Registry → address).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet_identity = models.ForeignKey(
        WalletIdentity,
        on_delete=models.CASCADE,
        related_name="addresses",
    )
    chain = models.CharField(
        max_length=32,
        choices=WalletChain.choices,
        default=WalletChain.POLYGON,
    )
    address = models.CharField(max_length=42)
    derivation_index = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16,
        choices=WalletAddressStatus.choices,
        default=WalletAddressStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["derivation_index"]
        verbose_name = "wallet address"
        verbose_name_plural = "wallet addresses"
        constraints = [
            models.UniqueConstraint(
                fields=["derivation_index"],
                name="wallet_address_derivation_index_unique",
            ),
            models.UniqueConstraint(
                fields=["wallet_identity", "chain", "address"],
                name="wallet_address_identity_chain_address_unique",
            ),
            models.UniqueConstraint(
                fields=["wallet_identity", "chain"],
                condition=models.Q(status=WalletAddressStatus.ACTIVE),
                name="wallet_address_one_active_per_identity_chain",
            ),
            models.CheckConstraint(
                condition=models.Q(derivation_index__gte=0),
                name="wallet_address_derivation_index_gte_0",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status=WalletAddressStatus.ACTIVE,
                        retired_at__isnull=True,
                    )
                    | models.Q(
                        status=WalletAddressStatus.RETIRED,
                        retired_at__isnull=False,
                    )
                ),
                name="wallet_address_retired_at_matches_status",
            ),
        ]
        indexes = [
            models.Index(fields=["address", "chain"], name="wallet_addr_address_chain"),
            models.Index(
                fields=["wallet_identity", "status"],
                name="wallet_addr_identity_status",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chain}:{self.address} ({self.status})"


class ObservationStatus(models.TextChoices):
    """Deposit Observation state machine (RFC 006).

    Cap 2 owns transitions through Confirmed / Rejected / Expired.
    Cap 3 owns Credit Conversion Started → Credited.
    """

    OBSERVED = "observed", "Observed"
    PENDING_CONFIRMATION = "pending_confirmation", "Pending Confirmation"
    CONFIRMED = "confirmed", "Confirmed"
    CONVERSION_STARTED = "conversion_started", "Credit Conversion Started"
    CREDITED = "credited", "Credited"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"


class DepositObservation(models.Model):
    """Inbound value observation keyed by Observation Identity (RFC 006).

    Observation Identity = ``chain`` + ``tx_hash`` + ``log_index``.
    Duplicate adapter signals must collapse to one row.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet_address = models.ForeignKey(
        WalletAddress,
        on_delete=models.PROTECT,
        related_name="observations",
    )
    chain = models.CharField(
        max_length=32,
        choices=WalletChain.choices,
        default=WalletChain.POLYGON,
    )
    tx_hash = models.CharField(max_length=66)
    log_index = models.PositiveIntegerField()
    status = models.CharField(
        max_length=32,
        choices=ObservationStatus.choices,
        default=ObservationStatus.OBSERVED,
    )
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    token_contract = models.CharField(max_length=42)
    from_address = models.CharField(max_length=42, blank=True, default="")
    confirmations = models.PositiveIntegerField(default=0)
    block_number = models.PositiveBigIntegerField(null=True, blank=True)
    status_reason = models.CharField(max_length=255, blank=True, default="")
    observed_at = models.DateTimeField(auto_now_add=True)
    pending_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    conversion_started_at = models.DateTimeField(null=True, blank=True)
    credited_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-observed_at"]
        verbose_name = "deposit observation"
        verbose_name_plural = "deposit observations"
        constraints = [
            models.UniqueConstraint(
                fields=["chain", "tx_hash", "log_index"],
                name="wallet_observation_identity_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="wallet_observation_amount_gt_0",
            ),
            models.CheckConstraint(
                condition=models.Q(log_index__gte=0),
                name="wallet_observation_log_index_gte_0",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "chain"], name="wallet_obs_status_chain"),
            models.Index(
                fields=["wallet_address", "status"],
                name="wallet_obs_address_status",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.chain}:{self.tx_hash}:{self.log_index} ({self.status})"
