"""Partner Channel schema (ADR 023).

Tables, constraints, and immutability only. No margin, grant, or attribution
services in this module.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
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


class PartnerInviteLinkQuerySet(RefuseDeleteQuerySet):
    """Block classification edits once a visit exists.

    Token regenerate stays allowed.
    """

    _FROZEN_FIELDS = frozenset(
        {"partner_channel", "partner_channel_id", "source", "campaign", "content"}
    )

    def update(self, **kwargs: Any) -> int:
        frozen = self._FROZEN_FIELDS & kwargs.keys()
        has_visit = self.filter(visits__isnull=False).exists()
        if frozen and has_visit:
            raise AppendOnlyViolation(
                "PartnerInviteLink partner_channel, source, campaign, and content "
                "are frozen after the first visit"
            )
        return super().update(**kwargs)


class PartnerInviteLinkManager(models.Manager.from_queryset(PartnerInviteLinkQuerySet)):
    """Invite links can be updated, except frozen fields after the first visit."""


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


_CHANNEL_OWNERSHIP_FIELDS = frozenset(
    {
        "kind",
        "owner_user",
        "owner_user_id",
        "organization",
        "organization_id",
    }
)


class PartnerChannelQuerySet(RefuseDeleteQuerySet):
    """Ownership is write-once. Activity and revenue share stay writable.

    Django 5.1 ``bulk_update`` calls ``update`` inside ``atomic(savepoint=False)``.
    Reject ownership fields here first so that refusal does not mark the
    surrounding transaction broken.
    """

    def update(self, **kwargs: Any) -> int:
        blocked = _CHANNEL_OWNERSHIP_FIELDS.intersection(kwargs)
        if blocked:
            raise AppendOnlyViolation(
                "PartnerChannel kind, owner_user, and organization are immutable"
            )
        return super().update(**kwargs)

    def bulk_update(self, objs, fields, batch_size=None):
        blocked = _CHANNEL_OWNERSHIP_FIELDS.intersection(fields)
        if blocked:
            raise AppendOnlyViolation(
                "PartnerChannel kind, owner_user, and organization are immutable"
            )
        return super().bulk_update(objs, fields, batch_size=batch_size)


class PartnerChannelManager(models.Manager.from_queryset(PartnerChannelQuerySet)):
    """Default manager. Blocks hard delete and ownership changes."""


class PartnerChannel(models.Model):
    """Partner program owned by one user or one Organization (ADR 024).

    Not a money owner. The settlement Account is resolved from the owner
    relation. This model has no Account foreign key.
    """

    class Kind(models.TextChoices):
        INDIVIDUAL = "individual", "Individual"
        TEAM = "team", "Team"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(
        max_length=16,
        choices=Kind.choices,
        default=Kind.TEAM,
        db_index=True,
    )
    owner_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="individual_partner_channel",
    )
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="partner_channel",
    )
    revenue_share_percent = models.DecimalField(max_digits=5, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PartnerChannelManager()

    class Meta:
        verbose_name = "partner channel"
        verbose_name_plural = "partner channels"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(revenue_share_percent__gte=0)
                & models.Q(revenue_share_percent__lte=100),
                name="billing_partner_channel_share_range",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=["individual", "team"]),
                name="billing_partner_channel_kind_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="individual",
                        owner_user__isnull=False,
                        organization__isnull=True,
                    )
                    | models.Q(
                        kind="team",
                        owner_user__isnull=True,
                        organization__isnull=False,
                    )
                ),
                name="billing_partner_channel_owner_xor",
            ),
        ]

    def __str__(self) -> str:
        return f"PartnerChannel {self.pk} ({self.kind})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            if _CHANNEL_OWNERSHIP_FIELDS.intersection(update_fields):
                raise AppendOnlyViolation(
                    "PartnerChannel kind, owner_user, and organization are immutable"
                )
        elif not self._state.adding:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("kind", "owner_user_id", "organization_id")
                .first()
            )
            if previous is not None and (
                previous["kind"] != self.kind
                or previous["owner_user_id"] != self.owner_user_id
                or previous["organization_id"] != self.organization_id
            ):
                raise AppendOnlyViolation(
                    "PartnerChannel kind, owner_user, and organization are immutable"
                )
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation(
            "PartnerChannel must not be hard-deleted; set is_active=False"
        )


class PartnerInviteLink(models.Model):
    """One invite link on a PartnerChannel.

    A channel may have many links. The portal canonical link is the row with
    the smallest ``(created_at, id)``, resolved by
    ``canonical_invite_link`` — not by an unordered ``.first()``.

    ``token`` is not a form field. Only regenerate writes it.

    After the first ``InviteVisit``, ``save`` and ``QuerySet.update`` reject
    changes to ``partner_channel``, ``source``, ``campaign``, and ``content``.
    ``name``, ``bonus_amount``, and ``is_active`` stay writable. Token changes
    only through regenerate.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    partner_channel = models.ForeignKey(
        PartnerChannel,
        on_delete=models.PROTECT,
        related_name="invite_links",
    )
    token = models.CharField(max_length=64, unique=True, editable=False)
    name = models.CharField(max_length=128, blank=True, default="")
    bonus_amount = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        default=Decimal("0.000000"),
    )
    source = models.CharField(max_length=64, blank=True, default="")
    campaign = models.CharField(max_length=64, blank=True, default="")
    content = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    regenerated_at = models.DateTimeField(null=True, blank=True)

    objects = PartnerInviteLinkManager()

    class Meta:
        verbose_name = "partner invite link"
        verbose_name_plural = "partner invite links"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(bonus_amount__gte=0),
                name="billing_invite_link_bonus_gte_0",
            ),
        ]

    _FROZEN_FIELDS = frozenset(
        {"partner_channel", "partner_channel_id", "source", "campaign", "content"}
    )

    def __str__(self) -> str:
        return f"PartnerInviteLink {self.partner_channel_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if self.pk and not self._state.adding and self._touches_frozen(update_fields):
            if self.visits.exists() and self._frozen_values_changed(update_fields):
                raise AppendOnlyViolation(
                    "PartnerInviteLink partner_channel, source, campaign, and content "
                    "are frozen after the first visit"
                )
        super().save(*args, **kwargs)

    def _touches_frozen(self, update_fields: Any) -> bool:
        if update_fields is None:
            return True
        return bool(self._FROZEN_FIELDS & set(update_fields))

    def _frozen_values_changed(self, update_fields: Any) -> bool:
        previous = type(self).objects.get(pk=self.pk)
        names = ["partner_channel_id", "source", "campaign", "content"]
        if update_fields is not None:
            names = []
            for name in update_fields:
                if name in ("partner_channel", "partner_channel_id"):
                    names.append("partner_channel_id")
                elif name in ("source", "campaign", "content"):
                    names.append(name)
        return any(getattr(self, name) != getattr(previous, name) for name in names)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("PartnerInviteLink must not be hard-deleted")


class InviteVisit(models.Model):
    """One immutable click on an invite link. No personal data."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invite_link = models.ForeignKey(
        PartnerInviteLink,
        on_delete=models.PROTECT,
        related_name="visits",
    )
    utm_source = models.CharField(max_length=128, blank=True, default="")
    utm_medium = models.CharField(max_length=128, blank=True, default="")
    utm_campaign = models.CharField(max_length=128, blank=True, default="")
    utm_content = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = RefuseDeleteManager()

    class Meta:
        verbose_name = "invite visit"
        verbose_name_plural = "invite visits"
        indexes = [
            models.Index(
                fields=["invite_link", "created_at"],
                name="bill_invite_visit_link_at",
            ),
        ]

    def __str__(self) -> str:
        return f"InviteVisit {self.pk}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if self.pk and not self._state.adding:
            raise AppendOnlyViolation("InviteVisit is immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise AppendOnlyViolation("InviteVisit must not be hard-deleted")


_ATTRIBUTION_SNAPSHOT_FIELDS = frozenset(
    {
        "invite_visit",
        "invite_visit_id",
        "invite_token",
        "registered_via_invite",
        "invite_name_snapshot",
        "invite_source_snapshot",
        "invite_campaign_snapshot",
        "invite_content_snapshot",
        "utm_source_snapshot",
        "utm_medium_snapshot",
        "utm_campaign_snapshot",
        "utm_content_snapshot",
        "bonus_amount_snapshot",
    }
)


class CustomerAttributionQuerySet(models.QuerySet):
    """Snapshot columns are write-once. ``partner_channel`` stays transferable."""

    def update(self, **kwargs: Any) -> int:
        blocked = _ATTRIBUTION_SNAPSHOT_FIELDS.intersection(kwargs)
        if blocked:
            raise AppendOnlyViolation(
                "CustomerAttribution invite snapshots are write-once"
            )
        return super().update(**kwargs)


class CustomerAttributionManager(
    models.Manager.from_queryset(CustomerAttributionQuerySet)
):
    """Default manager. Does not block a later channel transfer."""


class CustomerAttribution(models.Model):
    """A customer's single current partner (ADR 023). Not a Membership.

    ``invite_visit`` is the converting click. ``invite_token`` is only the
    token copied when the row was inserted. One user has one row; the visit
    itself is not unique.

    ``bonus_amount_snapshot``:

    - ``NULL`` — not eligible (existing account, admin, or a legacy row)
    - ``0.000000`` — new invite registration through a link whose bonus was 0
    - greater than 0 — new invite registration and the exact bonus for that row

    This model does not credit that amount.
    """

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
    invite_visit = models.ForeignKey(
        InviteVisit,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="customer_attributions",
    )
    invite_token = models.CharField(max_length=64, null=True, blank=True)
    registered_via_invite = models.BooleanField(default=False)
    invite_name_snapshot = models.CharField(max_length=128, blank=True, default="")
    invite_source_snapshot = models.CharField(max_length=64, blank=True, default="")
    invite_campaign_snapshot = models.CharField(max_length=64, blank=True, default="")
    invite_content_snapshot = models.CharField(max_length=64, blank=True, default="")
    utm_source_snapshot = models.CharField(max_length=128, blank=True, default="")
    utm_medium_snapshot = models.CharField(max_length=128, blank=True, default="")
    utm_campaign_snapshot = models.CharField(max_length=128, blank=True, default="")
    utm_content_snapshot = models.CharField(max_length=128, blank=True, default="")
    bonus_amount_snapshot = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    attributed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CustomerAttributionManager()

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

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if self.pk and not self._state.adding and self._snapshot_touched(update_fields):
            if self._snapshot_changed(update_fields):
                raise AppendOnlyViolation(
                    "CustomerAttribution invite snapshots are write-once"
                )
        super().save(*args, **kwargs)

    def _snapshot_touched(self, update_fields: Any) -> bool:
        if update_fields is None:
            return True
        return bool(_ATTRIBUTION_SNAPSHOT_FIELDS & set(update_fields))

    def _snapshot_changed(self, update_fields: Any) -> bool:
        previous = type(self).objects.get(pk=self.pk)
        names = _snapshot_attr_names(update_fields)
        return any(getattr(self, name) != getattr(previous, name) for name in names)


def _snapshot_attr_names(update_fields: Any) -> list[str]:
    names = [
        "invite_visit_id",
        "invite_token",
        "registered_via_invite",
        "invite_name_snapshot",
        "invite_source_snapshot",
        "invite_campaign_snapshot",
        "invite_content_snapshot",
        "utm_source_snapshot",
        "utm_medium_snapshot",
        "utm_campaign_snapshot",
        "utm_content_snapshot",
        "bonus_amount_snapshot",
    ]
    if update_fields is None:
        return names
    selected: list[str] = []
    for name in update_fields:
        if name in ("invite_visit", "invite_visit_id"):
            selected.append("invite_visit_id")
        elif name in names:
            selected.append(name)
    return selected


class PendingPartnerAttribution(models.Model):
    """Join context for an inactive user after register (ADR 023).

    ``invite_visit`` is the click the next service cut will confirm.
    ``invite_token_snapshot`` and ``partner_channel`` stay so rows written
    before that cut can still be matched. Snapshots are not stored here.
    """

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
    invite_visit = models.ForeignKey(
        InviteVisit,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="pending_attributions",
    )
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
    changed_by_user_id_snapshot = models.BigIntegerField()
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
    customer_user_id_snapshot = models.BigIntegerField()
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
    customer_user_id_snapshot = models.BigIntegerField()
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
    granted_by_user_id_snapshot = models.BigIntegerField()
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
