"""Order models."""

from django.db import models


class Order(models.Model):
    """A customer order for an eSIM package.

    Product snapshot fields are copied from the catalog Package at reserve
    time and must not be updated when the live catalog later changes.
    """

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
    # Immutable purchase-time product snapshot (not live catalog).
    package_title = models.CharField(max_length=255, blank=True, default="")
    operator_title = models.CharField(max_length=255, blank=True, default="")
    location_title = models.CharField(max_length=255, blank=True, default="")
    country_code = models.CharField(max_length=2, blank=True, default="")
    data_allowance = models.CharField(max_length=64, blank=True, default="")
    validity_days = models.PositiveIntegerField(null=True, blank=True)
    retail_price_usd = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="What the customer paid (credits). Immutable after create.",
    )
    currency = models.CharField(
        max_length=3,
        blank=True,
        default="",
        help_text="ISO 4217; copied from billing LEDGER_CURRENCY at purchase.",
    )
    net_price_usd = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Provider wholesale cost. Internal only — never expose via API.",
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
