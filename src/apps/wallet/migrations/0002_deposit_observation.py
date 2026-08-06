# DepositObservation schema — Observation Identity + state machine (RFC 006)

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0001_wallet_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="DepositObservation",
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
                (
                    "chain",
                    models.CharField(
                        choices=[("polygon", "Polygon")],
                        default="polygon",
                        max_length=32,
                    ),
                ),
                ("tx_hash", models.CharField(max_length=66)),
                ("log_index", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("observed", "Observed"),
                            ("pending_confirmation", "Pending Confirmation"),
                            ("confirmed", "Confirmed"),
                            ("conversion_started", "Credit Conversion Started"),
                            ("credited", "Credited"),
                            ("rejected", "Rejected"),
                            ("expired", "Expired"),
                        ],
                        default="observed",
                        max_length=32,
                    ),
                ),
                ("amount", models.DecimalField(decimal_places=6, max_digits=20)),
                ("token_contract", models.CharField(max_length=42)),
                (
                    "from_address",
                    models.CharField(blank=True, default="", max_length=42),
                ),
                ("confirmations", models.PositiveIntegerField(default=0)),
                ("block_number", models.PositiveBigIntegerField(blank=True, null=True)),
                (
                    "status_reason",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("observed_at", models.DateTimeField(auto_now_add=True)),
                ("pending_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("conversion_started_at", models.DateTimeField(blank=True, null=True)),
                ("credited_at", models.DateTimeField(blank=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, null=True)),
                ("expired_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "wallet_address",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="observations",
                        to="wallet.walletaddress",
                    ),
                ),
            ],
            options={
                "verbose_name": "deposit observation",
                "verbose_name_plural": "deposit observations",
                "ordering": ["-observed_at"],
            },
        ),
        # Align CheckConstraint deconstruct with Django 5.1 (no SQL change).
        migrations.RemoveConstraint(
            model_name="walletaddress",
            name="wallet_address_retired_at_matches_status",
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("retired_at__isnull", True), ("status", "active")),
                    models.Q(("retired_at__isnull", False), ("status", "retired")),
                    _connector="OR",
                ),
                name="wallet_address_retired_at_matches_status",
            ),
        ),
        migrations.AddIndex(
            model_name="depositobservation",
            index=models.Index(
                fields=["status", "chain"], name="wallet_obs_status_chain"
            ),
        ),
        migrations.AddIndex(
            model_name="depositobservation",
            index=models.Index(
                fields=["wallet_address", "status"],
                name="wallet_obs_address_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="depositobservation",
            constraint=models.UniqueConstraint(
                fields=("chain", "tx_hash", "log_index"),
                name="wallet_observation_identity_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="depositobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("amount__gt", 0)),
                name="wallet_observation_amount_gt_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="depositobservation",
            constraint=models.CheckConstraint(
                condition=models.Q(("log_index__gte", 0)),
                name="wallet_observation_log_index_gte_0",
            ),
        ),
    ]
