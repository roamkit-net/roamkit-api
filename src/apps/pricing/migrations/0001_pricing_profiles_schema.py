# Generated manually for ADR 019 / PR1 pricing profiles schema

import uuid
from decimal import Decimal

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PricingProfile",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=128)),
                ("slug", models.SlugField(max_length=128)),
                (
                    "discount_percent",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0.00"),
                        help_text="Percent off list_price (0–100).",
                        max_digits=5,
                    ),
                ),
                (
                    "floor_policy",
                    models.CharField(
                        choices=[
                            ("none", "None"),
                            ("wholesale", "Wholesale net floor"),
                        ],
                        default="wholesale",
                        max_length=32,
                    ),
                ),
                (
                    "version",
                    models.PositiveIntegerField(
                        default=1,
                        help_text=(
                            "Auto-incremented on material field changes; "
                            "optimistic lock."
                        ),
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                (
                    "effective_from",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                (
                    "effective_until",
                    models.DateTimeField(
                        blank=True,
                        help_text="Null means open-ended.",
                        null=True,
                    ),
                ),
                (
                    "archived_at",
                    models.DateTimeField(
                        blank=True,
                        db_index=True,
                        help_text="Soft-delete timestamp; null = not archived.",
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "pricing profile",
                "verbose_name_plural": "pricing profiles",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="PricingProfileAudit",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("reason", models.CharField(default="admin_edit", max_length=255)),
                ("old_values", models.JSONField(default=dict)),
                ("new_values", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pricing_profile_audits",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audit_entries",
                        to="pricing.pricingprofile",
                    ),
                ),
            ],
            options={
                "verbose_name": "pricing profile audit",
                "verbose_name_plural": "pricing profile audits",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="pricingprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(("discount_percent__gte", 0))
                & models.Q(("discount_percent__lte", 100)),
                name="pricing_profile_discount_percent_0_100",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricingprofile",
            constraint=models.CheckConstraint(
                condition=models.Q(("version__gte", 1)),
                name="pricing_profile_version_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="pricingprofile",
            constraint=models.UniqueConstraint(
                condition=models.Q(("archived_at__isnull", True)),
                fields=("slug",),
                name="pricing_profile_unique_active_slug",
            ),
        ),
    ]
