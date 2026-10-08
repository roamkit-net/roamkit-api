"""Partner Channel schema (ADR 023).

Tables, constraints, and immutability only. No margin, grant, or attribution
services in this module.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.db import models

from apps.billing.models import AppendOnlyViolation


class RefuseDeleteQuerySet(models.QuerySet):
    """Block hard deletes. Status or service flows are the only exit."""

    def delete(self) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation(f"{self.model.__name__} must not be hard-deleted")


class RefuseDeleteManager(models.Manager.from_queryset(RefuseDeleteQuerySet)):
    """Default manager for rows that cannot be deleted."""


class ImmutableQuerySet(RefuseDeleteQuerySet):
    """Block updates and deletes on append-only audit rows."""

    def update(self, **kwargs: Any) -> int:
        raise AppendOnlyViolation(f"{self.model.__name__} is immutable")


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):
    """Default manager for append-only partner audit rows."""


_RENEWAL_IMMUTABLE_FIELDS = frozenset(
    {
        "subscription",
        "subscription_id",
        "billing_date",
        "renewal_list_price_usd",
        "renewal_net_price_usd",
        "id",
        "created_at",
    }
)


class RenewalCycleQuerySet(RefuseDeleteQuerySet):
    """Allow status updates only. Prices and identity stay write-once."""

    def update(self, **kwargs: Any) -> int:
        blocked = _RENEWAL_IMMUTABLE_FIELDS.intersection(kwargs)
        if blocked:
            raise AppendOnlyViolation(
                "SubscriptionRenewalCycle prices and identity are write-once"
            )
        if "status" in kwargs and self.filter(status="renewed").exists():
            raise AppendOnlyViolation("A renewed SubscriptionRenewalCycle is terminal")
        return super().update(**kwargs)


class RenewalCycleManager(models.Manager.from_queryset(RenewalCycleQuerySet)):
    """Default manager for renewal-cycle rows."""


class PartnerChannel(models.Model):
    """One partner program on an existing Organization (ADR 023).

    Not a money owner. The team Account is ``organization.account``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="partner_channel",
    )
    revenue_share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RefuseDeleteManager()

    class Meta:
        verbose_name = "partner channel"
        verbose_name_plural = "partner channels"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revenue_share_percent__gte=0)
                & models.Q(revenue_share_percent__lte=100),
                name="billing_partner_channel_share_range",
            ),
        ]

    def __str__(self) -> str:
        return f"PartnerChannel {self.organization_id}"

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation(
            "PartnerChannel must not be hard-deleted; set is_active=False"
        )


class PartnerInviteLink(models.Model):
    """The one permanent invite link for a PartnerChannel (ADR 023)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner_channel = models.OneToOneField(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="invite_link",
    )
    token = models.CharField(max_length=64, unique=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    regenerated_at = models.DateTimeField(null=True, blank=True)

    objects = RefuseDeleteManager()

    class Meta:
        verbose_name = "partner invite link"
        verbose_name_plural = "partner invite links"

    def __str__(self) -> str:
        return f"PartnerInviteLink {self.partner_channel_id}"

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("PartnerInviteLink must not be hard-deleted")


class CustomerAttribution(models.Model):
    """A customer's single current partner (ADR 023). Not a Membership."""

    class Source(models.TextChoices):
        INVITE_LINK = "invite_link", "Invite link"
        ADMIN = "admin", "Admin"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="customer_attribution",
    )
    partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="customer_attributions",
    )
    source = models.CharField(max_length=16, choices=Source.choices)
    invite_link = models.ForeignKey(
        PartnerInviteLink,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="attributions",
    )
    invite_token = models.CharField(max_length=64, null=True, blank=True)
    attributed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "customer attribution"
        verbose_name_plural = "customer attributions"
        indexes = [
            models.Index(
                fields=["partner_channel", "attributed_at"],
                name="bill_attr_chan_attributed",
            ),
        ]

    def __str__(self) -> str:
        return f"CustomerAttribution {self.user_id}"


class PendingPartnerAttribution(models.Model):
    """Join context for an inactive user after register (ADR 023)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pending_partner_attribution",
    )
    partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="pending_attributions",
    )
    invite_token_snapshot = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "pending partner attribution"
        verbose_name_plural = "pending partner attributions"
        indexes = [
            models.Index(fields=["expires_at"], name="bill_pending_attr_expires"),
        ]

    def __str__(self) -> str:
        return f"PendingPartnerAttribution {self.user_id}"


class CustomerAttributionHistory(models.Model):
    """Append-only transfer of a customer from one partner to another."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="customer_attribution_history",
    )
    from_partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="attribution_history_from",
    )
    to_partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="attribution_history_to",
    )
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_attribution_changes",
    )
    changed_by_user_id_snapshot = models.UUIDField()
    change_reason = models.TextField()
    changed_at = models.DateTimeField()
    previous_attributed_at = models.DateTimeField()

    objects = ImmutableManager()

    class Meta:
        verbose_name = "customer attribution history"
        verbose_name_plural = "customer attribution histories"
        indexes = [
            models.Index(
                fields=["user", "changed_at"],
                name="bill_attr_history_user_at",
            ),
        ]

    def __str__(self) -> str:
        return f"CustomerAttributionHistory {self.user_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyViolation("CustomerAttributionHistory is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("CustomerAttributionHistory must not be hard-deleted")


class PartnerMarginAccrual(models.Model):
    """Immutable margin snapshot for one fulfilled commercial event."""

    class SourceType(models.TextChoices):
        ORDER = "order", "Order"
        TOPUP = "topup", "Top-up"
        SUBSCRIPTION = "subscription", "Subscription"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="margin_accruals",
    )
    customer_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_margin_accruals",
    )
    customer_user_id_snapshot = models.UUIDField()
    customer_attribution = models.ForeignKey(
        CustomerAttribution,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="margin_accruals",
    )
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    source_id = models.CharField(max_length=64)
    list_price = models.DecimalField(max_digits=20, decimal_places=6)
    net_price = models.DecimalField(max_digits=20, decimal_places=6)
    margin = models.DecimalField(max_digits=20, decimal_places=6)
    revenue_share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    partner_share = models.DecimalField(max_digits=20, decimal_places=6)
    ledger_entry = models.OneToOneField(
        "billing.CreditLedgerEntry",
        on_delete=models.PROTECT,
        related_name="partner_margin_accrual",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        verbose_name = "partner margin accrual"
        verbose_name_plural = "partner margin accruals"
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id"],
                name="billing_partner_accrual_source_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["partner_channel", "created_at"],
                name="bill_accrual_chan_created",
            ),
            models.Index(
                fields=["partner_channel", "customer_user"],
                name="bill_accrual_chan_customer",
            ),
            models.Index(fields=["customer_user"], name="bill_accrual_customer"),
        ]

    def __str__(self) -> str:
        return f"PartnerMarginAccrual {self.source_type}:{self.source_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyViolation("PartnerMarginAccrual is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("PartnerMarginAccrual must not be hard-deleted")


class PartnerCreditGrant(models.Model):
    """Immutable team-to-customer credit transfer (ADR 023)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="credit_grants",
    )
    customer_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_credit_grants",
    )
    customer_user_id_snapshot = models.UUIDField()
    customer_attribution = models.ForeignKey(
        CustomerAttribution,
        on_delete=models.PROTECT,
        related_name="credit_grants",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="partner_grants_given",
    )
    granted_by_user_id_snapshot = models.UUIDField()
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    idempotency_key = models.CharField(max_length=128)
    debit_ledger_entry = models.OneToOneField(
        "billing.CreditLedgerEntry",
        on_delete=models.PROTECT,
        related_name="partner_grant_debit",
    )
    credit_ledger_entry = models.OneToOneField(
        "billing.CreditLedgerEntry",
        on_delete=models.PROTECT,
        related_name="partner_grant_credit",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        verbose_name = "partner credit grant"
        verbose_name_plural = "partner credit grants"
        constraints = [
            models.UniqueConstraint(
                fields=["partner_channel", "idempotency_key"],
                name="billing_partner_grant_idem_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="billing_partner_grant_amount_gt_0",
            ),
        ]
        indexes = [
            models.Index(
                fields=["partner_channel", "created_at"],
                name="bill_grant_chan_created",
            ),
            models.Index(
                fields=["partner_channel", "customer_user"],
                name="bill_grant_chan_customer",
            ),
        ]

    def __str__(self) -> str:
        return f"PartnerCreditGrant {self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise AppendOnlyViolation("PartnerCreditGrant is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("PartnerCreditGrant must not be hard-deleted")


class SubscriptionRenewalCycle(models.Model):
    """Write-once price snapshot for one subscription billing date (ADR 023)."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RENEWED = "renewed", "Renewed"
        PAUSED = "paused", "Paused"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        "billing.Subscription",
        on_delete=models.PROTECT,
        related_name="renewal_cycles",
    )
    billing_date = models.DateField()
    renewal_list_price_usd = models.DecimalField(max_digits=20, decimal_places=6)
    renewal_net_price_usd = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = RenewalCycleManager()

    class Meta:
        verbose_name = "subscription renewal cycle"
        verbose_name_plural = "subscription renewal cycles"
        constraints = [
            models.UniqueConstraint(
                fields=["subscription", "billing_date"],
                name="billing_renewal_cycle_sub_date",
            ),
            models.UniqueConstraint(
                fields=["subscription"],
                condition=models.Q(status__in=["pending", "paused", "failed"]),
                name="billing_renewal_one_unfinished",
            ),
        ]

    def __str__(self) -> str:
        return f"RenewalCycle {self.subscription_id} {self.billing_date}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "status",
                    "subscription_id",
                    "billing_date",
                    "renewal_list_price_usd",
                    "renewal_net_price_usd",
                )
                .first()
            )
            if previous is None:
                raise AppendOnlyViolation("SubscriptionRenewalCycle does not exist")
            if previous["status"] == self.Status.RENEWED:
                raise AppendOnlyViolation(
                    "A renewed SubscriptionRenewalCycle is terminal"
                )
            identity_changed = (
                previous["subscription_id"] != self.subscription_id
                or previous["billing_date"] != self.billing_date
                or previous["renewal_list_price_usd"] != self.renewal_list_price_usd
                or previous["renewal_net_price_usd"] != self.renewal_net_price_usd
            )
            if identity_changed:
                raise AppendOnlyViolation(
                    "SubscriptionRenewalCycle prices and identity are write-once"
                )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("SubscriptionRenewalCycle must not be hard-deleted")
