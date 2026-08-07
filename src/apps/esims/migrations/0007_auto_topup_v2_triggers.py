# Auto Top-up v2: replace trigger_mode with expiry_enabled + usage_mode.

from django.db import migrations, models


def forwards_map_triggers(apps, schema_editor):
    Policy = apps.get_model("esims", "EsimAutoTopupPolicy")
    for policy in Policy.objects.all().iterator():
        mode = policy.trigger_mode
        if mode == "expiry":
            policy.expiry_enabled = True
            policy.usage_mode = "disabled"
            policy.threshold_mb = None
        elif mode == "usage_threshold":
            policy.expiry_enabled = False
            policy.usage_mode = "threshold"
        elif mode == "usage_zero":
            policy.expiry_enabled = False
            policy.usage_mode = "zero"
            policy.threshold_mb = None
        else:
            raise ValueError(f"Unexpected trigger_mode on policy {policy.pk}: {mode!r}")
        policy.save(
            update_fields=[
                "expiry_enabled",
                "usage_mode",
                "threshold_mb",
            ]
        )


def backwards_map_triggers(apps, schema_editor):
    Policy = apps.get_model("esims", "EsimAutoTopupPolicy")
    for policy in Policy.objects.all().iterator():
        if policy.expiry_enabled and policy.usage_mode == "disabled":
            policy.trigger_mode = "expiry"
            policy.threshold_mb = None
        elif not policy.expiry_enabled and policy.usage_mode == "threshold":
            policy.trigger_mode = "usage_threshold"
        elif not policy.expiry_enabled and policy.usage_mode == "zero":
            policy.trigger_mode = "usage_zero"
            policy.threshold_mb = None
        elif policy.expiry_enabled:
            # Combo rows: lossy rollback → expiry (v2 design lock).
            policy.trigger_mode = "expiry"
            policy.threshold_mb = None
        elif policy.usage_mode == "threshold":
            policy.trigger_mode = "usage_threshold"
        elif policy.usage_mode == "zero":
            policy.trigger_mode = "usage_zero"
            policy.threshold_mb = None
        else:
            policy.trigger_mode = "expiry"
            policy.threshold_mb = None
        policy.save(update_fields=["trigger_mode", "threshold_mb"])


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0006_esim_auto_topup_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="esimautotopuppolicy",
            name="expiry_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="esimautotopuppolicy",
            name="usage_mode",
            field=models.CharField(
                choices=[
                    ("disabled", "Disabled"),
                    ("threshold", "Threshold"),
                    ("zero", "Zero"),
                ],
                default="disabled",
                max_length=32,
            ),
        ),
        migrations.RunPython(forwards_map_triggers, backwards_map_triggers),
        migrations.RemoveConstraint(
            model_name="esimautotopuppolicy",
            name="esims_auto_topup_threshold_required",
        ),
        migrations.RemoveField(
            model_name="esimautotopuppolicy",
            name="trigger_mode",
        ),
        migrations.AlterField(
            model_name="esimautotopuppolicy",
            name="threshold_mb",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Required when usage_mode is threshold.",
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="esimautotopuppolicy",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(usage_mode="threshold")
                    | models.Q(threshold_mb__isnull=False)
                ),
                name="esims_auto_topup_threshold_required",
            ),
        ),
    ]
