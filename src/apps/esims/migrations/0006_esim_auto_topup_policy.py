# Generated manually for EsimAutoTopupPolicy (design lock PR2).

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_account_pricing_profile"),
        ("esims", "0005_topup_pricing_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="EsimAutoTopupPolicy",
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
                ("package_id", models.CharField(db_index=True, max_length=64)),
                ("enabled", models.BooleanField(db_index=True, default=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("paused", "Paused"),
                            ("blocked", "Blocked"),
                            ("disabled", "Disabled"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=32,
                    ),
                ),
                (
                    "reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("insufficient_funds", "Insufficient funds"),
                            ("package_unavailable", "Package unavailable"),
                            ("usage_unknown", "Usage unknown"),
                            ("provider_error", "Provider error"),
                            ("manual_pause", "Manual pause"),
                            ("count_exhausted", "Count exhausted"),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                (
                    "trigger_mode",
                    models.CharField(
                        choices=[
                            ("usage_zero", "Usage zero"),
                            ("usage_threshold", "Usage threshold"),
                            ("expiry", "Expiry"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "threshold_mb",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Required when trigger_mode is usage_threshold.",
                        null=True,
                    ),
                ),
                (
                    "renew_mode",
                    models.CharField(
                        choices=[
                            ("until_funds", "Until funds"),
                            ("fixed_count", "Fixed count"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "remaining_count",
                    models.PositiveIntegerField(
                        blank=True,
                        help_text="Required when renew_mode is fixed_count.",
                        null=True,
                    ),
                ),
                ("cooldown_until", models.DateTimeField(blank=True, null=True)),
                ("last_triggered_at", models.DateTimeField(blank=True, null=True)),
                (
                    "last_idempotency_key",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("version", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="auto_topup_policies",
                        to="billing.account",
                    ),
                ),
                (
                    "esim",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="auto_topup_policies",
                        to="esims.esim",
                    ),
                ),
                (
                    "last_topup",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="esims.topup",
                    ),
                ),
            ],
            options={
                "verbose_name": "eSIM auto top-up policy",
                "verbose_name_plural": "eSIM auto top-up policies",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="esimautotopuppolicy",
            index=models.Index(
                fields=["status", "enabled"],
                name="esims_esima_status_5d29c0_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="esimautotopuppolicy",
            index=models.Index(
                fields=["account", "status"],
                name="esims_esima_account_975ac9_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimautotopuppolicy",
            constraint=models.UniqueConstraint(
                fields=("esim",),
                name="esims_auto_topup_policy_esim_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimautotopuppolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("trigger_mode", "usage_threshold"), _negated=True),
                    ("threshold_mb__isnull", False),
                    _connector="OR",
                ),
                name="esims_auto_topup_threshold_required",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimautotopuppolicy",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("renew_mode", "fixed_count"), _negated=True),
                    ("remaining_count__isnull", False),
                    _connector="OR",
                ),
                name="esims_auto_topup_count_required",
            ),
        ),
    ]
