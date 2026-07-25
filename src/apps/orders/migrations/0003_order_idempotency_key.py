"""Add unique nullable idempotency_key to Order (spend retries)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0002_billing_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="idempotency_key",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=128,
                null=True,
                unique=True,
            ),
        ),
    ]
