"""Organization + Membership collaboration models (ADR 020).

Organization is the collaboration aggregate — not a money or inventory owner.
``billing.Account`` remains the sole financial owner (ADR 010). Each
Organization has exactly one team Account (``kind=organization``).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class HardDeleteViolation(Exception):
    """Raised when a soft-lifecycle entity is hard-deleted."""


class SoftLifecycleQuerySet(models.QuerySet):
    """Block QuerySet.delete(); use status transitions (ADR 020)."""

    def delete(self) -> tuple[int, dict[str, int]]:
        raise HardDeleteViolation(
            f"{self.model.__name__} must not be hard-deleted; use status transitions"
        )


class SoftLifecycleManager(models.Manager.from_queryset(SoftLifecycleQuerySet)):
    """Default manager for soft-lifecycle Organization / Membership rows."""


class OrganizationStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    ARCHIVED = "archived", "Archived"


class MembershipRole(models.TextChoices):
    OWNER = "owner", "Owner"
    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


class MembershipStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    REVOKED = "revoked", "Revoked"


class Organization(models.Model):
    """Collaboration aggregate for a team (ADR 020).

    Not a financial owner. Money and (later) eSIM inventory live on
    ``account`` (``billing.Account`` with ``kind=organization``).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16,
        choices=OrganizationStatus.choices,
        default=OrganizationStatus.ACTIVE,
        db_index=True,
    )
    account = models.OneToOneField(
        "billing.Account",
        on_delete=models.PROTECT,
        related_name="organization",
        help_text=(
            "Dedicated team billing Account (kind=organization). "
            "Never a personal Account."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftLifecycleManager()

    class Meta:
        ordering = ["name"]
        verbose_name = "organization"
        verbose_name_plural = "organizations"
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        OrganizationStatus.ACTIVE,
                        OrganizationStatus.SUSPENDED,
                        OrganizationStatus.ARCHIVED,
                    ]
                ),
                name="organizations_organization_status_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.status})"

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation(
            "Organization must not be hard-deleted; use status transitions"
        )


class Membership(models.Model):
    """User membership in an Organization (ADR 020).

    At most one row per ``(user, organization)``. At most one active owner per
    organization (DB). Exactly-one-owner after mutations is enforced by
    ``services.membership``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(
        max_length=16,
        choices=MembershipRole.choices,
        default=MembershipRole.MEMBER,
    )
    status = models.CharField(
        max_length=16,
        choices=MembershipStatus.choices,
        default=MembershipStatus.ACTIVE,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftLifecycleManager()

    class Meta:
        ordering = ["organization_id", "created_at"]
        verbose_name = "membership"
        verbose_name_plural = "memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="organizations_membership_org_user_uniq",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=Q(
                    role=MembershipRole.OWNER,
                    status=MembershipStatus.ACTIVE,
                ),
                name="organizations_membership_one_active_owner",
            ),
            models.CheckConstraint(
                condition=Q(
                    role__in=[
                        MembershipRole.OWNER,
                        MembershipRole.ADMIN,
                        MembershipRole.MEMBER,
                        MembershipRole.VIEWER,
                    ]
                ),
                name="organizations_membership_role_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        MembershipStatus.ACTIVE,
                        MembershipStatus.SUSPENDED,
                        MembershipStatus.REVOKED,
                    ]
                ),
                name="organizations_membership_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "status"],
                name="organizations_memb_user_status",
            ),
            models.Index(
                fields=["organization", "status"],
                name="organizations_memb_org_status",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Membership {self.user_id} @ {self.organization_id} "
            f"({self.role}/{self.status})"
        )

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation(
            "Membership must not be hard-deleted; use status transitions"
        )
