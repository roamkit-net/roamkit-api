# Generated manually for Faza 1 catalog

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies: list[tuple[str, str]] = []

    operations = [
        migrations.CreateModel(
            name="Package",
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
                (
                    "external_id",
                    models.CharField(db_index=True, max_length=64, unique=True),
                ),
                ("title", models.CharField(max_length=255)),
                ("operator_title", models.CharField(max_length=255)),
                ("operator_id", models.CharField(blank=True, max_length=64)),
                (
                    "country_code",
                    models.CharField(blank=True, db_index=True, max_length=2),
                ),
                ("data_allowance", models.CharField(max_length=64)),
                ("validity_days", models.PositiveIntegerField()),
                ("price_usd", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "net_price_usd",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=10, null=True
                    ),
                ),
                ("is_unlimited", models.BooleanField(default=False)),
                ("plan_type", models.CharField(default="data", max_length=32)),
                ("source", models.CharField(default="airalo", max_length=32)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("synced_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["country_code", "price_usd", "title"],
                "indexes": [
                    models.Index(
                        fields=["is_active", "country_code"],
                        name="catalog_pac_is_acti_0a8f9d_idx",
                    ),
                ],
            },
        ),
    ]
