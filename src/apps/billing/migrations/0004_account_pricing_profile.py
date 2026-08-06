# ADR 019 — Account.pricing_profile FK

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0003_voucher_admin_audit"),
        ("pricing", "0001_pricing_profiles_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="pricing_profile",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Optional shared pricing profile (ADR 019). "
                    "Null = retail list price."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="accounts",
                to="pricing.pricingprofile",
            ),
        ),
    ]
