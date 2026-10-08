from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_google_oauth_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="display_name",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=255
            ),
        ),
    ]
