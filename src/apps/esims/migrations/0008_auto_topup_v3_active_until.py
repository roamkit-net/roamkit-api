# Auto Top-up v3: optional active_until lifetime + schedule_ended reason.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0007_auto_topup_v2_triggers"),
    ]

    operations = [
        migrations.AddField(
            model_name="esimautotopuppolicy",
            name="active_until",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Optional UTC exclusive end bound for policy lifetime (v3). "
                    "Null means no schedule limit."
                ),
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="esimautotopuppolicy",
            name="reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("insufficient_funds", "Insufficient funds"),
                    ("package_unavailable", "Package unavailable"),
                    ("usage_unknown", "Usage unknown"),
                    ("provider_error", "Provider error"),
                    ("manual_pause", "Manual pause"),
                    ("count_exhausted", "Count exhausted"),
                    ("schedule_ended", "Schedule ended"),
                ],
                default="",
                max_length=32,
            ),
        ),
    ]
