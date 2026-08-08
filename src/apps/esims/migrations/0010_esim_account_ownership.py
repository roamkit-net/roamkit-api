# Esim.account inventory ownership + assigned_user (ADR 020 / PR4)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_esim_accounts(apps, schema_editor):
    """Point each eSIM at its user's personal billing Account.

    Does not move inventory to a different owner — only attaches the Account
    that already corresponds to ``Esim.user``.
    """
    Esim = apps.get_model("esims", "Esim")
    Account = apps.get_model("billing", "Account")
    for esim in Esim.objects.filter(account__isnull=True).iterator():
        account = (
            Account.objects.filter(user_id=esim.user_id, kind="personal").first()
            or Account.objects.filter(user_id=esim.user_id).first()
        )
        if account is None:
            raise RuntimeError(
                f"No billing Account for Esim {esim.pk} user_id={esim.user_id}; "
                "refusing to invent a new inventory owner."
            )
        esim.account_id = account.pk
        esim.save(update_fields=["account_id"])


def noop_reverse(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    Esim.objects.update(account=None)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_account_kind"),
        ("esims", "0009_esim_archived_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="esim",
            name="account",
            field=models.ForeignKey(
                blank=True,
                help_text="Inventory owner (personal or organization Account).",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="esims",
                to="billing.account",
            ),
        ),
        migrations.AddField(
            model_name="esim",
            name="assigned_user",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Optional assignee (who uses the SIM). Not inventory or "
                    "financial owner — never used for authz or spend."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_esims",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(backfill_esim_accounts, noop_reverse),
        migrations.AlterField(
            model_name="esim",
            name="account",
            field=models.ForeignKey(
                help_text="Inventory owner (personal or organization Account).",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="esims",
                to="billing.account",
            ),
        ),
        migrations.AlterField(
            model_name="esim",
            name="user",
            field=models.ForeignKey(
                help_text=(
                    "Legacy dual-read owner link (personal Account user). "
                    "Do not use as inventory SoT — prefer ``account``."
                ),
                on_delete=django.db.models.deletion.CASCADE,
                related_name="esims",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddIndex(
            model_name="esim",
            index=models.Index(
                fields=["account", "status"],
                name="esims_esim_account_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="esim",
            index=models.Index(
                fields=["account", "archived_at"],
                name="esims_esim_account_arch_idx",
            ),
        ),
    ]
