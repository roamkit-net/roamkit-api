# User-local eSIM note (never synced to Airalo).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0003_esim_lifecycle_wave1"),
    ]

    operations = [
        migrations.AddField(
            model_name="esim",
            name="note",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
