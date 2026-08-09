# Account.kind + nullable user for organization Accounts (ADR 020 / PR2)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_account_pricing_profile"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="kind",
            field=models.CharField(
                choices=[
                    ("personal", "Personal"),
                    ("organization", "Organization"),
                ],
                db_index=True,
                default="personal",
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="account",
            name="user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="billing_account",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=models.Q(kind__in=["personal", "organization"]),
                name="billing_account_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(kind="personal", user__isnull=False)
                    | models.Q(kind="organization", user__isnull=True)
                ),
                name="billing_account_kind_user_consistency",
            ),
        ),
    ]
