"""Pricing profiles — product charge policy (ADR 019). Not a money path."""

from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify


class FloorPolicy(models.TextChoices):
    NONE = "none", "None"
    WHOLESALE = "wholesale", "Wholesale net floor"
    # Reserved (ADR 019) — do not implement behavior until Accepted follow-up:
    # WHOLESALE_PLUS_MARGIN = "wholesale_plus_margin", ...
    # FIXED_MIN_MARGIN = "fixed_min_margin", ...


class PricingProfile(models.Model):
    """Shared discount / floor policy assigned to billing Accounts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    slug = models.SlugField(max_length=128)
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Percent off list_price (0–100).",
    )
    floor_policy = models.CharField(
        max_length=32,
        choices=FloorPolicy.choices,
        default=FloorPolicy.WHOLESALE,
    )
    version = models.PositiveIntegerField(
        default=1,
        help_text="Auto-incremented on material field changes; optimistic lock.",
    )
    is_active = models.BooleanField(default=True)
    effective_from = models.DateTimeField(default=timezone.now)
    effective_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Null means open-ended.",
    )
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Soft-delete timestamp; null = not archived.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "pricing profile"
        verbose_name_plural = "pricing profiles"
        constraints = [
            models.CheckConstraint(
                condition=Q(discount_percent__gte=0) & Q(discount_percent__lte=100),
                name="pricing_profile_discount_percent_0_100",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1),
                name="pricing_profile_version_gte_1",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(archived_at__isnull=True),
                name="pricing_profile_unique_active_slug",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.slug}) v{self.version}"

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None

    def clean(self) -> None:
        super().clean()
        if self.effective_until and self.effective_from:
            if self.effective_until <= self.effective_from:
                raise ValidationError(
                    {"effective_until": "Must be after effective_from."}
                )
        if self.floor_policy not in {
            FloorPolicy.NONE,
            FloorPolicy.WHOLESALE,
        }:
            raise ValidationError(
                {"floor_policy": "Only none and wholesale are implemented in v1."}
            )

    def archive(self, *, at=None) -> None:
        """Soft-delete: archive and deactivate. Idempotent."""
        if self.archived_at is not None:
            return
        self.archived_at = at or timezone.now()
        self.is_active = False
        self.save(update_fields=["archived_at", "is_active", "updated_at"])

    def save(self, *args, **kwargs):
        if not self.slug and self.name:
            self.slug = slugify(self.name)[:128]
        update_fields = kwargs.get("update_fields")
        if self.pk and update_fields is None:
            try:
                previous = (
                    PricingProfile.objects.filter(pk=self.pk)
                    .values("discount_percent", "floor_policy", "version")
                    .get()
                )
            except PricingProfile.DoesNotExist:
                previous = None
            if previous is not None:
                material_changed = (
                    previous["discount_percent"] != self.discount_percent
                    or previous["floor_policy"] != self.floor_policy
                )
                if material_changed:
                    self.version = previous["version"] + 1
        super().save(*args, **kwargs)

    def save_optimistic(
        self, *, expected_version: int, update_fields: list[str]
    ) -> bool:
        """Update only if ``version`` still equals ``expected_version``.

        Material fields in ``update_fields`` trigger ``version`` bump in SQL.
        Returns True if a row was updated.
        """
        fields = set(update_fields)
        material = fields & {"discount_percent", "floor_policy"}
        now = timezone.now()
        updates: dict = {f: getattr(self, f) for f in fields}
        updates["updated_at"] = now
        if material:
            updates["version"] = expected_version + 1
            self.version = expected_version + 1
        updated = PricingProfile.objects.filter(
            pk=self.pk, version=expected_version
        ).update(**updates)
        if updated:
            for f, value in updates.items():
                setattr(self, f, value)
        return bool(updated)


class PricingProfileAudit(models.Model):
    """Admin change audit: who / when / old / new / reason (ADR 019)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(
        PricingProfile,
        on_delete=models.CASCADE,
        related_name="audit_entries",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pricing_profile_audits",
    )
    reason = models.CharField(max_length=255, default="admin_edit")
    old_values = models.JSONField(default=dict)
    new_values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "pricing profile audit"
        verbose_name_plural = "pricing profile audits"

    def __str__(self) -> str:
        return f"Audit {self.pk} profile={self.profile_id}"


def assign_pricing_profile(
    *,
    account_ids: list,
    profile: PricingProfile | None,
    actor=None,
    reason: str = "bulk_assign",
) -> int:
    """Assign ``profile`` to accounts in one transaction.

    ``profile=None`` clears the assignment. Returns number of accounts updated.
    Caller should pass ≤100 ids per call for large sets (chunk outside).
    """
    from apps.billing.models import Account

    if len(account_ids) > 100:
        raise ValidationError(
            "Bulk assign accepts at most 100 accounts per transaction."
        )
    if profile is not None and profile.archived_at is not None:
        raise ValidationError("Cannot assign an archived pricing profile.")

    with transaction.atomic():
        updated = Account.objects.filter(pk__in=account_ids).update(
            pricing_profile=profile
        )
        if actor is not None and profile is not None:
            PricingProfileAudit.objects.create(
                profile=profile,
                actor=actor,
                reason=reason,
                old_values={},
                new_values={
                    "assigned_account_ids": [str(x) for x in account_ids],
                    "updated_count": updated,
                },
            )
        return updated
