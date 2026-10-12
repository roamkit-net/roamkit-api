# Operator snapshot, initial-credit sequence, and Airalo package storage.

import django.db.models.deletion
from django.db import migrations, models


def copy_operator_and_number_topups(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    Topup = apps.get_model("esims", "Topup")
    Package = apps.get_model("catalog", "Package")

    for esim in Esim.objects.all().iterator():
        order = esim.order
        package = Package.objects.filter(pk=order.package_id).first()
        title = (order.operator_title or "").strip()
        if not title and package is not None:
            title = (package.operator_title or "").strip()
        if title and esim.operator_title != title:
            esim.operator_title = title
            esim.save(update_fields=["operator_title"])

        key = f"initial-credit:{esim.pk}"
        amount = order.retail_price_usd
        if amount is None or amount <= 0:
            amount = package.price_usd if package is not None else None
        external_id = (order.package_external_id or "").strip()
        if not external_id and package is not None:
            external_id = package.external_id
        if (
            amount is not None
            and amount > 0
            and external_id
            and not Topup.objects.filter(idempotency_key=key).exists()
        ):
            created = Topup.objects.create(
                account_id=esim.account_id,
                esim_id=esim.pk,
                package_external_id=external_id,
                amount=amount,
                status="fulfilled",
                external_order_id=(order.external_order_id or "")[:64],
                idempotency_key=key,
                sequence=0,
                list_price_usd=order.list_price_usd,
            )
            Topup.objects.filter(pk=created.pk).update(created_at=order.created_at)

        sequence = 1
        existing = (
            Topup.objects.filter(esim_id=esim.pk)
            .exclude(idempotency_key=key)
            .order_by("created_at", "pk")
        )
        for topup in existing:
            if topup.sequence != sequence:
                topup.sequence = sequence
                topup.save(update_fields=["sequence"])
            sequence += 1


def remove_initial_credits(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    Topup = apps.get_model("esims", "Topup")
    Topup.objects.filter(idempotency_key__startswith="initial-credit:").delete()
    Esim.objects.update(operator_title="")


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0005_package_activation_policy"),
        ("orders", "0007_order_coverage_snapshot"),
        ("esims", "0012_partner_channel_schema"),
    ]

    operations = [
        migrations.CreateModel(
            name="EsimAppliedPackage",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("instance_id", models.CharField(max_length=64)),
                ("sequence", models.PositiveIntegerField()),
                (
                    "package_external_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("kind", models.CharField(blank=True, default="", max_length=16)),
                ("status", models.CharField(blank=True, default="", max_length=32)),
                (
                    "data_allowance",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("validity_days", models.PositiveIntegerField(default=0)),
                ("is_unlimited", models.BooleanField(default=False)),
                (
                    "amount_mb",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                ("remaining_mb", models.IntegerField(blank=True, null=True)),
                ("activated_at", models.DateTimeField(blank=True, null=True)),
                ("expired_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "provider_price_usd",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        help_text=(
                            "Airalo package.price. Not the customer charge "
                            "and not net_price."
                        ),
                        max_digits=10,
                        null=True,
                    ),
                ),
                ("synced_at", models.DateTimeField()),
            ],
            options={
                "ordering": ["sequence", "instance_id"],
            },
        ),
        migrations.AddField(
            model_name="esim",
            name="operator_title",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text="Purchase-time operator name copied from the order snapshot.",
                max_length=255,
                verbose_name="Operator",
            ),
        ),
        migrations.AddField(
            model_name="topup",
            name="provider_package_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("active", "Active"),
                    ("not_active", "Not active"),
                    ("finished", "Finished"),
                    ("expired", "Expired"),
                    ("unknown", "Unknown"),
                ],
                default="",
                help_text="Airalo package status. Blank until a usage refresh stores it.",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="topup",
            name="sequence",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="0 is the initial credit. Later top-ups of this eSIM are 1, 2, 3, …",
                null=True,
            ),
        ),
        migrations.RunPython(
            copy_operator_and_number_topups,
            remove_initial_credits,
        ),
        migrations.AddConstraint(
            model_name="topup",
            constraint=models.UniqueConstraint(
                fields=("esim", "sequence"),
                name="esims_topup_esim_sequence_uniq",
            ),
        ),
        migrations.AddField(
            model_name="esimappliedpackage",
            name="esim",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="applied_packages",
                to="esims.esim",
            ),
        ),
        migrations.AddField(
            model_name="esimappliedpackage",
            name="topup",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="applied_package",
                to="esims.topup",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimappliedpackage",
            constraint=models.UniqueConstraint(
                fields=("esim", "instance_id"),
                name="esims_applied_pkg_esim_instance_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimappliedpackage",
            constraint=models.UniqueConstraint(
                fields=("esim", "sequence"),
                name="esims_applied_pkg_esim_sequence_uniq",
            ),
        ),
    ]
