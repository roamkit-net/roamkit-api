# The sequence constraint cannot share 0013's transaction: Postgres refuses
# to alter esims_topup while that migration's column backfill still has
# pending trigger events.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0013_esim_operator_and_packages"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="topup",
            constraint=models.UniqueConstraint(
                fields=("esim", "sequence"),
                name="esims_topup_esim_sequence_uniq",
            ),
        ),
    ]
