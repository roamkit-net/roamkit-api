# Wallet schema — WalletIdentity + WalletAddress (Index Registry)

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("billing", "0003_voucher_admin_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletIdentity",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wallet_identity",
                        to="billing.account",
                    ),
                ),
            ],
            options={
                "verbose_name": "wallet identity",
                "verbose_name_plural": "wallet identities",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="WalletAddress",
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
                ("address", models.CharField(max_length=42)),
                ("derivation_index", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("retired", "Retired")],
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("retired_at", models.DateTimeField(blank=True, null=True)),
                (
                    "wallet_identity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="addresses",
                        to="wallet.walletidentity",
                    ),
                ),
            ],
            options={
                "verbose_name": "wallet address",
                "verbose_name_plural": "wallet addresses",
                "ordering": ["derivation_index"],
            },
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.UniqueConstraint(
                fields=("derivation_index",),
                name="wallet_address_derivation_index_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.UniqueConstraint(
                fields=("wallet_identity", "chain", "address"),
                name="wallet_address_identity_chain_address_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "active")),
                fields=("wallet_identity", "chain"),
                name="wallet_address_one_active_per_identity_chain",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.CheckConstraint(
                condition=models.Q(("derivation_index__gte", 0)),
                name="wallet_address_derivation_index_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletaddress",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("retired_at__isnull", True), ("status", "active"))
                    | models.Q(("retired_at__isnull", False), ("status", "retired"))
                ),
                name="wallet_address_retired_at_matches_status",
            ),
        ),
        migrations.AddIndex(
            model_name="walletaddress",
            index=models.Index(
                fields=["address", "chain"],
                name="wallet_addr_address_chain",
            ),
        ),
        migrations.AddIndex(
            model_name="walletaddress",
            index=models.Index(
                fields=["wallet_identity", "status"],
                name="wallet_addr_identity_status",
            ),
        ),
    ]
