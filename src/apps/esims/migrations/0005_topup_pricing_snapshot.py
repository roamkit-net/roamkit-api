# ADR 019 PR2 — Topup pricing snapshot fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0004_esim_note"),
    ]

    operations = [
        migrations.AddField(
            model_name="topup",
            name="list_price_usd",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Provider list price at purchase (ADR 019).",
                max_digits=10,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="topup",
            name="discount_percent",
            field=models.DecimalField(
                blank=True, decimal_places=2, max_digits=5, null=True
            ),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_reason",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="topup",
            name="floor_reason",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_profile_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_profile_version",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_profile_slug",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_profile_name",
            field=models.CharField(blank=True, default="", max_length=128),
        ),
        migrations.AddField(
            model_name="topup",
            name="pricing_context_hash",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="topup",
            name="snapshot_schema_version",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
