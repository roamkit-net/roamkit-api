from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0005_order_pricing_snapshot"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="coverage_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Purchase-time location coverage snapshot: local | regional | "
                    "global. Empty for legacy orders; never re-read from live "
                    "catalog at status time."
                ),
                max_length=16,
            ),
        ),
    ]
