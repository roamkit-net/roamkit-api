# Faza 5 Wave 1: Esim lifecycle status, setup fields, snapshot, events.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def remap_unused_to_purchased(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    Esim.objects.filter(status="unused").update(status="purchased")
    Esim.objects.filter(status="active").update(status="activated")


def noop_reverse(apps, schema_editor):
    Esim = apps.get_model("esims", "Esim")
    Esim.objects.filter(status="purchased").update(status="unused")
    Esim.objects.filter(status="activated").update(status="active")


class Migration(migrations.Migration):

    dependencies = [
        ("esims", "0002_topup_model"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="esim",
            name="activation_policy",
            field=models.CharField(
                choices=[
                    ("first_usage", "First usage"),
                    ("installation", "Installation"),
                    ("unknown", "Unknown"),
                ],
                default="unknown",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="esim",
            name="setup_version",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="esim",
            name="setup_resume_step",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="esim",
            name="setup_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="esim",
            name="setup_skipped_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="esim",
            name="status",
            field=models.CharField(
                choices=[
                    ("purchased", "Purchased"),
                    ("installation_started", "Installation started"),
                    ("installed", "Installed"),
                    ("activated", "Activated"),
                    ("in_use", "In use"),
                    ("exhausted", "Exhausted"),
                    ("expired", "Expired"),
                    ("unknown", "Unknown"),
                ],
                db_index=True,
                default="purchased",
                max_length=32,
            ),
        ),
        migrations.RunPython(remap_unused_to_purchased, noop_reverse),
        migrations.CreateModel(
            name="EsimLifecycleEvent",
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
                ("event_type", models.CharField(db_index=True, max_length=64)),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("client", "Client"),
                            ("system", "System"),
                            ("provider", "Provider"),
                        ],
                        default="client",
                        max_length=16,
                    ),
                ),
                ("schema_version", models.PositiveSmallIntegerField(default=1)),
                ("idempotency_key", models.CharField(max_length=128)),
                (
                    "setup_session_id",
                    models.UUIDField(blank=True, db_index=True, null=True),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("user_agent", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "esim",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lifecycle_events",
                        to="esims.esim",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="esim_lifecycle_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="esimlifecycleevent",
            index=models.Index(
                fields=["esim", "created_at"],
                name="esims_esiml_esim_id_6f0a1c_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="esimlifecycleevent",
            index=models.Index(
                fields=["event_type", "created_at"],
                name="esims_esiml_event_t_8b2d4e_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="esimlifecycleevent",
            constraint=models.UniqueConstraint(
                fields=("esim", "idempotency_key"),
                name="esims_lifecycle_event_esim_idem_uniq",
            ),
        ),
    ]
