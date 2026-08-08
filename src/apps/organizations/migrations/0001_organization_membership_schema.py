# Organization + Membership schema (ADR 020 / PR1)

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Organization",
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
                ("name", models.CharField(max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("suspended", "Suspended"),
                            ("archived", "Archived"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "organization",
                "verbose_name_plural": "organizations",
                "ordering": ["name"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("status__in", ["active", "suspended", "archived"])
                        ),
                        name="organizations_organization_status_valid",
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Membership",
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
                    "role",
                    models.CharField(
                        choices=[
                            ("owner", "Owner"),
                            ("admin", "Admin"),
                            ("member", "Member"),
                            ("viewer", "Viewer"),
                        ],
                        default="member",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("suspended", "Suspended"),
                            ("revoked", "Revoked"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="organization_memberships",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="memberships",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "verbose_name": "membership",
                "verbose_name_plural": "memberships",
                "ordering": ["organization_id", "created_at"],
                "indexes": [
                    models.Index(
                        fields=["user", "status"], name="organizations_memb_user_status"
                    ),
                    models.Index(
                        fields=["organization", "status"],
                        name="organizations_memb_org_status",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "user"),
                        name="organizations_membership_org_user_uniq",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("role", "owner"), ("status", "active")),
                        fields=("organization",),
                        name="organizations_membership_one_active_owner",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("role__in", ["owner", "admin", "member", "viewer"])
                        ),
                        name="organizations_membership_role_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("status__in", ["active", "suspended", "revoked"])
                        ),
                        name="organizations_membership_status_valid",
                    ),
                ],
            },
        ),
    ]
