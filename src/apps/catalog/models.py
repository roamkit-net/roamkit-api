"""Catalog models."""

from django.db import models


class Package(models.Model):
    """Cached eSIM package synced from an external provider."""

    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    operator_title = models.CharField(max_length=255)
    operator_id = models.CharField(max_length=64, blank=True)
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    data_allowance = models.CharField(max_length=64)
    validity_days = models.PositiveIntegerField()
    price_usd = models.DecimalField(max_digits=10, decimal_places=2)
    net_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    is_unlimited = models.BooleanField(default=False)
    plan_type = models.CharField(max_length=32, default="data")
    source = models.CharField(max_length=32, default="airalo")
    is_active = models.BooleanField(default=True, db_index=True)
    synced_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["country_code", "price_usd", "title"]
        indexes = [
            models.Index(
                fields=["is_active", "country_code"],
                name="catalog_pac_is_acti_0a8f9d_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.external_id})"
