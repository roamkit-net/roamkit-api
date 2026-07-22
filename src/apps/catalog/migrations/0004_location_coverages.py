# Generated manually for Location.coverages JSON field.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0003_package_voice_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="coverages",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
