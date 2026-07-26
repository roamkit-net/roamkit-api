"""Order models."""

from django.db import models


class Order(models.Model):
    """A customer order for an eSIM package."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        PAID = "paid", "Paid"
        FULFILLING = "fulfilling", "Fulfilling"
        FULFILLED = "fulfilled", "Fulfilled"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    account = models.ForeignKey(
        "billing.Account",
        on_delete=models.CASCADE,
        related_name="orders",
    )
    package = models.ForeignKey(
        "catalog.Package",
        on_delete=models.PROTECT,
        related_name="orders",
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
        help_text="Order lifecycle status (draft → fulfilled / failed / cancelled).",
    )
    external_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    customer_ref = models.CharField(max_length=255, blank=True)
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
        indexes = [
            models.Index(fields=["account", "status"]),
        ]

    def __str__(self) -> str:
        return f"Order {self.pk} ({self.status})"
