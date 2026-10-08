# User.id is a bigint. Snapshot columns that store a user id match that type.
# Drop and re-add: Postgres cannot cast uuid to bigint. These tables have no
# writers yet, so the columns are empty.

from django.db import migrations, models


def _require_empty(apps, schema_editor):
    pairs = (
        ("CustomerAttributionHistory",),
        ("PartnerMarginAccrual",),
        ("PartnerCreditGrant",),
    )
    for (model_name,) in pairs:
        model = apps.get_model("billing", model_name)
        if model.objects.exists():
            raise RuntimeError(
                f"Cannot change {model_name} user-id snapshot columns while rows exist"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0006_partner_channel_schema"),
    ]

    operations = [
        migrations.RunPython(_require_empty, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="customerattributionhistory",
            name="changed_by_user_id_snapshot",
        ),
        migrations.AddField(
            model_name="customerattributionhistory",
            name="changed_by_user_id_snapshot",
            field=models.BigIntegerField(),
        ),
        migrations.RemoveField(
            model_name="partnermarginaccrual",
            name="customer_user_id_snapshot",
        ),
        migrations.AddField(
            model_name="partnermarginaccrual",
            name="customer_user_id_snapshot",
            field=models.BigIntegerField(),
        ),
        migrations.RemoveField(
            model_name="partnercreditgrant",
            name="customer_user_id_snapshot",
        ),
        migrations.AddField(
            model_name="partnercreditgrant",
            name="customer_user_id_snapshot",
            field=models.BigIntegerField(),
        ),
        migrations.RemoveField(
            model_name="partnercreditgrant",
            name="granted_by_user_id_snapshot",
        ),
        migrations.AddField(
            model_name="partnercreditgrant",
            name="granted_by_user_id_snapshot",
            field=models.BigIntegerField(),
        ),
    ]
