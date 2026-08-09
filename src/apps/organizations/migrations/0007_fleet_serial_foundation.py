# ADR 021 Option C′ foundation: uem_serial_number + fleet credentials.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("organizations", "0006_device_binding_uem_device_guid"),
    ]

    operations = [
        migrations.AddField(
            model_name="devicebinding",
            name="uem_serial_number",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text=(
                    "Stable UEM serialNumber bootstrap identity (ADR 021 Option C′). "
                    "Maps to App Config roamkit.device_serial=%SerialNumber%. "
                    "Not an auth secret. Empty = PR18-only binding without fleet serial."
                ),
                max_length=128,
            ),
        ),
        migrations.AlterField(
            model_name="devicebinding",
            name="uem_device_guid",
            field=models.CharField(
                blank=True,
                db_index=True,
                default="",
                help_text=(
                    "Current BlackBerry UEM device.guid correlation/cache "
                    "(ADR 021 Option C′). Refreshable after re-enroll when serial "
                    "match is unique. Not an auth secret. Empty = classic PR18 path "
                    "or guid not yet resolved."
                ),
                max_length=64,
            ),
        ),
        migrations.AddConstraint(
            model_name="devicebinding",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "active"))
                & ~models.Q(("uem_serial_number", "")),
                fields=("organization", "uem_serial_number"),
                name="organizations_devicebinding_one_active_serial_per_org",
            ),
        ),
        migrations.CreateModel(
            name="OrganizationFleetCredential",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "fleet_external_id",
                    models.CharField(
                        db_index=True,
                        help_text=(
                            "Opaque fleet lookup id for UEM App Config "
                            "(not Organization.id)."
                        ),
                        max_length=64,
                        unique=True,
                    ),
                ),
                (
                    "current_credential_hash",
                    models.CharField(
                        help_text=(
                            "SHA-256 hex of current fleet credential; "
                            "plaintext never stored."
                        ),
                        max_length=64,
                    ),
                ),
                ("current_issued_at", models.DateTimeField()),
                (
                    "previous_credential_hash",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text=(
                            "Prior credential hash valid only until "
                            "previous_valid_until."
                        ),
                        max_length=64,
                    ),
                ),
                (
                    "previous_valid_until",
                    models.DateTimeField(
                        blank=True,
                        help_text=(
                            "Grace window end for previous_credential_hash; "
                            "null = no grace."
                        ),
                        null=True,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fleet_credential",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "organization fleet credential",
                "verbose_name_plural": "organization fleet credentials",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="FleetCredentialEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "action",
                    models.CharField(
                        choices=[
                            (
                                "fleet_credential_issued",
                                "Fleet credential issued",
                            ),
                            (
                                "fleet_credential_rotated",
                                "Fleet credential rotated",
                            ),
                        ],
                        max_length=32,
                    ),
                ),
                ("fleet_external_id", models.CharField(max_length=64)),
                (
                    "previous_valid_until",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="fleet_credential_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "fleet_credential",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="events",
                        to="organizations.organizationfleetcredential",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fleet_credential_events",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "fleet credential event",
                "verbose_name_plural": "fleet credential events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="fleetcredentialevent",
            index=models.Index(
                fields=["organization", "created_at"],
                name="org_fcevt_org_created",
            ),
        ),
        migrations.AddConstraint(
            model_name="organizationfleetcredential",
            constraint=models.CheckConstraint(
                condition=~models.Q(("fleet_external_id", "")),
                name="organizations_fleetcred_external_id_nonempty",
            ),
        ),
        migrations.AddConstraint(
            model_name="organizationfleetcredential",
            constraint=models.CheckConstraint(
                condition=~models.Q(("current_credential_hash", "")),
                name="organizations_fleetcred_current_hash_nonempty",
            ),
        ),
    ]
