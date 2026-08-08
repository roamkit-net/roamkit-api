# Organization.account → team billing.Account (ADR 020 / PR2)

import uuid
from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def backfill_organization_accounts(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Account = apps.get_model("billing", "Account")
    now = timezone.now()
    for org in Organization.objects.filter(account__isnull=True).iterator():
        account = Account.objects.create(
            id=uuid.uuid4(),
            kind="organization",
            user=None,
            balance=Decimal("0"),
            version=0,
            created_at=now,
            updated_at=now,
        )
        org.account = account
        org.save(update_fields=["account"])


def noop_reverse(apps, schema_editor):
    # Keep team Accounts; clearing Organization.account is enough for reverse.
    Organization = apps.get_model("organizations", "Organization")
    Organization.objects.update(account=None)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0005_account_kind"),
        ("organizations", "0001_organization_membership_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="account",
            field=models.OneToOneField(
                help_text=(
                    "Dedicated team billing Account (kind=organization). "
                    "Never a personal Account."
                ),
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="organization",
                to="billing.account",
            ),
        ),
        migrations.RunPython(backfill_organization_accounts, noop_reverse),
        migrations.AlterField(
            model_name="organization",
            name="account",
            field=models.OneToOneField(
                help_text=(
                    "Dedicated team billing Account (kind=organization). "
                    "Never a personal Account."
                ),
                on_delete=django.db.models.deletion.PROTECT,
                related_name="organization",
                to="billing.account",
            ),
        ),
    ]
