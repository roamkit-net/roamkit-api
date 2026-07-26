# Faza 5 Wave 1: Package.activation_policy

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0004_location_coverages"),
    ]

    operations = [
        migrations.AddField(
            model_name="package",
            name="activation_policy",
            field=models.CharField(
                db_index=True,
                default="unknown",
                help_text="first_usage | installation | unknown (synced from provider).",
                max_length=32,
            ),
        ),
    ]
