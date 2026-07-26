"""Catalog models."""

from django.db import models


class Location(models.Model):
    """A catalog destination (local country, regional, or global)."""

    COVERAGE_LOCAL = "local"
    COVERAGE_REGIONAL = "regional"
    COVERAGE_GLOBAL = "global"
    COVERAGE_CHOICES = [
        (COVERAGE_LOCAL, "Local"),
        (COVERAGE_REGIONAL, "Regional"),
        (COVERAGE_GLOBAL, "Global"),
    ]

    slug = models.SlugField(max_length=128, unique=True)
    title = models.CharField(max_length=255)
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    coverage_type = models.CharField(
        max_length=16, choices=COVERAGE_CHOICES, db_index=True
    )
    image_url = models.URLField(blank=True, max_length=512)
    covered_country_codes = models.JSONField(default=list, blank=True)
    # [{code, name, networks: [{name, types}]}] from provider operator.coverages
    coverages = models.JSONField(default=list, blank=True)
    is_popular = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        indexes = [
            models.Index(
                fields=["coverage_type", "is_popular"],
                name="catalog_loc_coverage_pop_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.slug})"


class Package(models.Model):
    """Cached eSIM package synced from an external provider."""

    external_id = models.CharField(max_length=64, unique=True, db_index=True)
    title = models.CharField(max_length=255)
    operator_title = models.CharField(max_length=255)
    operator_id = models.CharField(max_length=64, blank=True)
    country_code = models.CharField(max_length=2, blank=True, db_index=True)
    location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="packages",
    )
    data_allowance = models.CharField(max_length=64)
    validity_days = models.PositiveIntegerField()
    price_usd = models.DecimalField(max_digits=10, decimal_places=2)
    net_price_usd = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    is_unlimited = models.BooleanField(default=False)
    plan_type = models.CharField(max_length=32, default="data")
    voice_minutes = models.PositiveIntegerField(null=True, blank=True)
    text_sms = models.PositiveIntegerField(null=True, blank=True)
    activation_policy = models.CharField(
        max_length=32,
        default="unknown",
        db_index=True,
        help_text="first_usage | installation | unknown (synced from provider).",
    )
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
