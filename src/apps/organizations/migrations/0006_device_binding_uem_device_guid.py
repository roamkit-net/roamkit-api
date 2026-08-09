# Generated manually for ADR 021 staging UEM ICCID lookup proof.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0005_device_binding_credential"),
    ]

    operations = [
        migrations.AddField(
            model_name="devicebinding",
            name="uem_device_guid",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text=(
                    "BlackBerry UEM device.guid bridge for staging SIM/ICCID lookup "
                    "(ADR 021 option C proof). Empty = classic PR18 binding.esim path. "
                    "Not an auth secret."
                ),
                max_length=64,
            ),
        ),
    ]
