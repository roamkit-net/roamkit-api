# PartnerChannel kind and owner shape (ADR 024).
#
# Conditionally reversible while every row is still a legacy TEAM channel:
# kind='team', owner_user NULL, organization set. Reverse refuses otherwise
# and does not delete INDIVIDUAL channels to make a downgrade succeed.
#
# RunPython uses historical models only. It does not import live billing,
# user, or organization classes, and it does not call partner services.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count, Exists, OuterRef


def _preflight(apps, schema_editor):
    """Read-only. Fail closed. Do not repair rows."""
    PartnerChannel = apps.get_model("billing", "PartnerChannel")
    CustomerAttribution = apps.get_model("billing", "CustomerAttribution")
    Membership = apps.get_model("organizations", "Membership")

    # Defensive only. The legacy column is NOT NULL and OneToOne, so these
    # shapes cannot be produced by the application. They mean corruption.
    missing_organization = list(
        PartnerChannel.objects.filter(organization_id__isnull=True).values_list(
            "id", flat=True
        )[:20]
    )
    if missing_organization:
        raise RuntimeError(
            "Defensive legacy check failed: PartnerChannel rows have a null "
            "organization_id. That is structurally impossible under the current "
            f"NOT NULL column. Not repairing. Rows: {missing_organization}"
        )

    duplicate_organizations = list(
        PartnerChannel.objects.values("organization_id")
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .values_list("organization_id", "row_count")[:20]
    )
    if duplicate_organizations:
        raise RuntimeError(
            "Defensive legacy check failed: multiple PartnerChannel rows share "
            "an organization_id. That is structurally impossible under the "
            f"current OneToOne. Not repairing. Rows: {duplicate_organizations}"
        )

    # CustomerAttribution is the single current attribution per user.
    # CustomerAttributionHistory and PendingPartnerAttribution are not current.
    active_member = Membership.objects.filter(
        user_id=OuterRef("user_id"),
        organization_id=OuterRef("partner_channel__organization_id"),
        status="active",
    )
    conflicts = CustomerAttribution.objects.filter(Exists(active_member))
    if conflicts.exists():
        details = []
        for attribution in conflicts.select_related("partner_channel")[:20]:
            roles = list(
                Membership.objects.filter(
                    user_id=attribution.user_id,
                    organization_id=attribution.partner_channel.organization_id,
                    status="active",
                ).values_list("role", flat=True)
            )
            details.append(
                {
                    "partner_channel_id": str(attribution.partner_channel_id),
                    "organization_id": str(attribution.partner_channel.organization_id),
                    "user_id": attribution.user_id,
                    "roles": roles,
                }
            )
        raise RuntimeError(
            "TEAM self-attribution conflict: an active organization member is "
            "the current customer of that organization's PartnerChannel. "
            "This migration does not delete attribution, change membership, "
            f"or move money. Resolve the attribution explicitly, then rerun. "
            f"Conflicts: {details}"
        )


def _backfill_team(apps, schema_editor):
    """Stamp every existing channel as TEAM. Do not touch organization."""
    PartnerChannel = apps.get_model("billing", "PartnerChannel")
    PartnerChannel.objects.all().update(kind="team", owner_user_id=None)


def _validate_team_backfill(apps, schema_editor):
    PartnerChannel = apps.get_model("billing", "PartnerChannel")
    illegal = PartnerChannel.objects.exclude(
        kind="team",
        owner_user_id__isnull=True,
        organization_id__isnull=False,
    )
    if illegal.exists():
        sample = list(
            illegal.values_list("id", "kind", "owner_user_id", "organization_id")[:20]
        )
        raise RuntimeError(
            "PartnerChannel backfill left rows that are not legacy TEAM. "
            f"Not repairing. Rows: {sample}"
        )


def _noop(apps, schema_editor):
    return None


def _refuse_reverse_unless_legacy_team(apps, schema_editor):
    """Run before any column is dropped. Never delete post-0012 channels."""
    PartnerChannel = apps.get_model("billing", "PartnerChannel")
    illegal = PartnerChannel.objects.exclude(
        kind="team",
        owner_user_id__isnull=True,
        organization_id__isnull=False,
    )
    if illegal.exists():
        sample = list(
            illegal.values_list("id", "kind", "owner_user_id", "organization_id")[:20]
        )
        raise RuntimeError(
            "billing.0012 is conditionally reversible only while every "
            "PartnerChannel is still a legacy TEAM row (kind='team', "
            "owner_user NULL, organization set). Refusing to destroy "
            f"INDIVIDUAL channels or other post-0012 ownership. Rows: {sample}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0011_partner_invite_bonus"),
        ("organizations", "0007_fleet_serial_foundation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(_preflight, _noop),
        migrations.AddField(
            model_name="partnerchannel",
            name="kind",
            field=models.CharField(
                choices=[("individual", "Individual"), ("team", "Team")],
                db_index=True,
                default="team",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="partnerchannel",
            name="owner_user",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="individual_partner_channel",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(_backfill_team, _noop),
        migrations.RunPython(_validate_team_backfill, _noop),
        migrations.AlterField(
            model_name="partnerchannel",
            name="organization",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="partner_channel",
                to="organizations.organization",
            ),
        ),
        migrations.AddConstraint(
            model_name="partnerchannel",
            constraint=models.CheckConstraint(
                condition=models.Q(kind__in=["individual", "team"]),
                name="billing_partner_channel_kind_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="partnerchannel",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="individual",
                        owner_user__isnull=False,
                        organization__isnull=True,
                    )
                    | models.Q(
                        kind="team",
                        owner_user__isnull=True,
                        organization__isnull=False,
                    )
                ),
                name="billing_partner_channel_owner_xor",
            ),
        ),
        # Reverse of this no-op runs first and refuses non-legacy rows
        # before organization is set NOT NULL or the new columns are dropped.
        migrations.RunPython(_noop, _refuse_reverse_unless_legacy_team),
    ]
