# User-local eSIM archive visibility (never synced to Airalo).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0008_auto_topup_v3_active_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="esim",
            name="archived_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "User-local visibility only. Must never affect lifecycle, "
                    "top-up, billing, auto-topup, provider sync, usage refresh, "
                    "or events."
                ),
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="esim",
            index=models.Index(
                fields=["user", "archived_at"],
                name="esims_esim_user_id_415443_idx",
            ),
        ),
    ]
