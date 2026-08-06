# ADR 019 PR2 — Order pricing snapshot + package_external_id

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0004_order_product_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="package_external_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="order",
            name="list_price_usd",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Provider list / recommended retail at purchase (ADR 019).",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="discount_percent",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_reason",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="order",
            name="floor_reason",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_profile_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_profile_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_profile_slug",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_profile_name",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="order",
            name="pricing_context_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="order",
            name="snapshot_schema_version",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
