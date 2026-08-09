from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0006_order_coverage_type_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="coverage_snapshot",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text=(
                    "Purchase-time stable coverage list "
                    "[{country_code, country_name, operators}]. "
                    "null = legacy (never backfilled from live catalog)."
                ),
                null=True,
            ),
        ),
    ]
