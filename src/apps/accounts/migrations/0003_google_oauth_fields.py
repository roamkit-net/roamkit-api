# Generated manually for ADR 015 Google OAuth fields.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_billing_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="google_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="user",
            name="google_picture",
            field=models.URLField(blank=True, default="", max_length=2048),
        ),
        migrations.AddField(
            model_name="user",
            name="google_sub",
            field=models.CharField(
                blank=True, db_index=True, max_length=255, null=True, unique=True
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="last_google_login_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="last_login_provider",
            field=models.CharField(
                blank=True,
                choices=[("password", "password"), ("google", "google")],
                max_length=32,
                null=True,
            ),
        ),
    ]
