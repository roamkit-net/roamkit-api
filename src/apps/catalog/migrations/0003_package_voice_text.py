# Generated manually for voice_minutes / text_sms package fields.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0002_location_and_package_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="package",
            name="voice_minutes",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="package",
            name="text_sms",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
