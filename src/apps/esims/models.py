"""eSIM models."""

import uuid

from django.conf import settings
from django.db import models


class Esim(models.Model):
    """A provisioned eSIM owned by a user."""

    class Status(models.TextChoices):
        UNUSED = "unused", "Unused"
        ACTIVE = "active", "Active"
        EXHAUSTED = "exhausted", "Exhausted"
        EXPIRED = "expired", "Expired"
        UNKNOWN = "unknown", "Unknown"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="esims",
    )
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="esims",
    )
    iccid = models.CharField(max_length=32, unique=True, db_index=True)
    lpa = models.CharField(max_length=255, blank=True)
    matching_id = models.CharField(max_length=128, blank=True)
    qrcode = models.TextField(blank=True)
    qrcode_url = models.URLField(max_length=512, blank=True)
    direct_apple_installation_url = models.URLField(max_length=1024, blank=True)
    manual_installation = models.TextField(blank=True)
    qrcode_installation = models.TextField(blank=True)
    installation_guide_url = models.URLField(max_length=512, blank=True)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.UNUSED,
        db_index=True,
    )
    # Usage cache — populated by UsageService on usage fetch.
    usage_remaining_mb = models.IntegerField(null=True, blank=True)
    usage_total_mb = models.IntegerField(null=True, blank=True)
    usage_status = models.CharField(max_length=64, blank=True)
    usage_is_unlimited = models.BooleanField(null=True, blank=True)
    usage_expired_at = models.DateTimeField(null=True, blank=True)
    usage_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self) -> str:
        return f"eSIM {self.iccid}"


class Topup(models.Model):
    """A prepaid credit spend that applies a top-up package to an eSIM."""

    class Status(models.TextChoices):
        FULFILLING = "fulfilling", "Fulfilling"
        FULFILLED = "fulfilled", "Fulfilled"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="topups",
    )
    esim = models.ForeignKey(
        Esim,
        on_delete=models.CASCADE,
        related_name="topups",
    )
    package_external_id = models.CharField(max_length=64, db_index=True)
    amount = models.DecimalField(max_digits=20, decimal_places=6)
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.FULFILLING,
        db_index=True,
    )
    external_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    idempotency_key = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "top-up"
        verbose_name_plural = "top-ups"
        indexes = [
            models.Index(fields=["account", "status"]),
            models.Index(fields=["esim", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="esims_topup_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"Topup {self.pk} ({self.status})"
