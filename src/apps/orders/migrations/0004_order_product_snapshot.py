"""Add immutable product snapshot fields on Order and backfill from Package."""

from django.db import migrations, models


def forwards_backfill_product_snapshots(apps, schema_editor):
    Order = apps.get_model("orders", "Order")
    # Import runtime helper so backfill rules stay in one place (tolerant,
    # idempotent). Historical Order model is compatible with the queryset API.
    from apps.orders.product_snapshot import backfill_order_product_snapshots

    backfill_order_product_snapshots(Order)


def backwards_noop(apps, schema_editor):
    # Snapshot columns are dropped with the schema reverse; no data rewrite.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0003_order_idempotency_key"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="country_code",
            field=models.CharField(blank=True, default="", max_length=2),
        ),
        migrations.AddField(
            model_name="order",
            name="currency",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "ISO 4217; copied from billing LEDGER_CURRENCY at purchase."
                ),
                max_length=3,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="data_allowance",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="order",
            name="location_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="order",
            name="net_price_usd",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=(
                    "Provider wholesale cost. Internal only — never expose via API."
                ),
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="operator_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="order",
            name="package_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="order",
            name="retail_price_usd",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text=("What the customer paid (credits). Immutable after create."),
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="validity_days",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(
            forwards_backfill_product_snapshots,
            backwards_noop,
        ),
    ]
