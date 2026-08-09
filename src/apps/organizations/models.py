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


class InviteStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REVOKED = "revoked", "Revoked"
    EXPIRED = "expired", "Expired"


class InviteRole(models.TextChoices):
    """Roles that may be granted via invite (owner is never inviteable)."""

    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"
    VIEWER = "viewer", "Viewer"


class OrganizationInvite(models.Model):
    """Email invite into an Organization (ADR 020).

    Token plaintext is returned only at create/rotate time; only ``token_hash``
    is stored. Accept must not merge wallets or move eSIM inventory.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="invites",
    )
    email = models.EmailField(help_text="Invitee email as entered (display).")
    email_normalized = models.EmailField(
        db_index=True,
        help_text="Trimmed lowercase email for uniqueness / match.",
    )
    role = models.CharField(
        max_length=16,
        choices=InviteRole.choices,
        default=InviteRole.MEMBER,
    )
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
        db_index=True,
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_invites_sent",
    )
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="organization_invites_accepted",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftLifecycleManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "organization invite"
        verbose_name_plural = "organization invites"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email_normalized"],
                condition=Q(status=InviteStatus.PENDING),
                name="organizations_invite_one_pending_per_email",
            ),
            models.CheckConstraint(
                condition=Q(
                    role__in=[
                        InviteRole.ADMIN,
                        InviteRole.MEMBER,
                        InviteRole.VIEWER,
                    ]
                ),
                name="organizations_invite_role_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        InviteStatus.PENDING,
                        InviteStatus.ACCEPTED,
                        InviteStatus.REVOKED,
                        InviteStatus.EXPIRED,
                    ]
                ),
                name="organizations_invite_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status"],
                name="organizations_inv_org_status",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Invite {self.email_normalized} @ {self.organization_id} "
            f"({self.role}/{self.status})"
        )

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation(
            "OrganizationInvite must not be hard-deleted; use status transitions"
        )


class DeviceBindingStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    UNBOUND = "unbound", "Unbound"
    REPLACED = "replaced", "Replaced"


class DeviceBinding(models.Model):
    """Bind a team Account eSIM to a RoamKit-issued device external id (ADR 020).

    v1 cardinality: one **active** binding per eSIM, and one **active**
    ``device_external_id`` per Organization. ``device_external_id`` is never an
    authorization source. Personal-Account eSIMs are out of scope for v1.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="device_bindings",
    )
    esim = models.ForeignKey(
        "esims.Esim",
        on_delete=models.PROTECT,
        related_name="device_bindings",
    )
    device_external_id = models.CharField(
        max_length=64,
        db_index=True,
        help_text="RoamKit-issued opaque device key (not a client authz signal).",
    )
    uem_serial_number = models.CharField(
        max_length=128,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Stable UEM serialNumber bootstrap identity (ADR 021 Option C′). "
            "Maps to App Config roamkit.device_serial=%SerialNumber%. "
            "Not an auth secret. Empty = PR18-only binding without fleet serial."
        ),
    )
    uem_device_guid = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text=(
            "Current BlackBerry UEM device.guid correlation/cache "
            "(ADR 021 Option C′). Refreshable after re-enroll when serial "
            "match is unique. Not an auth secret. Empty = classic PR18 path "
            "or guid not yet resolved."
        ),
    )
    credential_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        db_index=True,
        help_text="SHA-256 hex of device credential; plaintext never stored.",
    )
    credential_issued_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=DeviceBindingStatus.choices,
        default=DeviceBindingStatus.ACTIVE,
        db_index=True,
    )
    bound_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_bindings_created",
    )
    unbound_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_bindings_unbound",
    )
    unbound_at = models.DateTimeField(null=True, blank=True)
    replaced_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftLifecycleManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "device binding"
        verbose_name_plural = "device bindings"
        constraints = [
            models.UniqueConstraint(
                fields=["esim"],
                condition=Q(status=DeviceBindingStatus.ACTIVE),
                name="organizations_devicebinding_one_active_per_esim",
            ),
            models.UniqueConstraint(
                fields=["organization", "device_external_id"],
                condition=Q(status=DeviceBindingStatus.ACTIVE),
                name="organizations_devicebinding_one_active_device_per_org",
            ),
            models.UniqueConstraint(
                fields=["organization", "uem_serial_number"],
                condition=(
                    Q(status=DeviceBindingStatus.ACTIVE) & ~Q(uem_serial_number="")
                ),
                name="organizations_devicebinding_one_active_serial_per_org",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=[
                        DeviceBindingStatus.ACTIVE,
                        DeviceBindingStatus.UNBOUND,
                        DeviceBindingStatus.REPLACED,
                    ]
                ),
                name="organizations_devicebinding_status_valid",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "status"],
                name="org_dbind_org_status",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"DeviceBinding {self.device_external_id} "
            f"esim={self.esim_id} ({self.status})"
        )

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation(
            "DeviceBinding must not be hard-deleted; use status transitions"
        )


class DeviceBindingEventAction(models.TextChoices):
    BIND = "bind", "Bind"
    UNBIND = "unbind", "Unbind"
    REBIND = "rebind", "Rebind"
    CREDENTIAL_ISSUE = "credential_issue", "Credential issue"
    CREDENTIAL_ROTATE = "credential_rotate", "Credential rotate"


class DeviceBindingEvent(models.Model):
    """Append-only audit trail for DeviceBinding mutations (ADR 020)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="device_binding_events",
    )
    binding = models.ForeignKey(
        DeviceBinding,
        on_delete=models.CASCADE,
        related_name="events",
    )
    esim = models.ForeignKey(
        "esims.Esim",
        on_delete=models.PROTECT,
        related_name="device_binding_events",
    )
    action = models.CharField(max_length=32, choices=DeviceBindingEventAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="device_binding_events",
    )
    device_external_id = models.CharField(max_length=64)
    previous_binding = models.ForeignKey(
        DeviceBinding,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="superseded_by_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "device binding event"
        verbose_name_plural = "device binding events"
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="org_dbevt_org_created",
            ),
        ]

    def __str__(self) -> str:
        return f"DeviceBindingEvent {self.action} {self.device_external_id}"

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation("DeviceBindingEvent must not be hard-deleted")


class FleetCredentialEventAction(models.TextChoices):
    ISSUE = "fleet_credential_issued", "Fleet credential issued"
    ROTATE = "fleet_credential_rotated", "Fleet credential rotated"


class OrganizationFleetCredential(models.Model):
    """Org-scoped fleet App Config credentials (ADR 021 Option C′).

    ``fleet_external_id`` is an opaque public lookup key — never the Organization
    UUID. Plaintext secrets exist only at issue/rotate; hashes at rest.
    ``previous_credential_hash`` remains valid until ``previous_valid_until``.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        Organization,
        on_delete=models.CASCADE,
        related_name="fleet_credential",
    )
    fleet_external_id = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="Opaque fleet lookup id for UEM App Config (not Organization.id).",
    )
    current_credential_hash = models.CharField(
        max_length=64,
        help_text="SHA-256 hex of current fleet credential; plaintext never stored.",
    )
    current_issued_at = models.DateTimeField()
    previous_credential_hash = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Prior credential hash valid only until previous_valid_until.",
    )
    previous_valid_until = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Grace window end for previous_credential_hash; null = no grace.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SoftLifecycleManager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "organization fleet credential"
        verbose_name_plural = "organization fleet credentials"
        constraints = [
            models.CheckConstraint(
                condition=~Q(fleet_external_id=""),
                name="organizations_fleetcred_external_id_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(current_credential_hash=""),
                name="organizations_fleetcred_current_hash_nonempty",
            ),
        ]

    def __str__(self) -> str:
        return f"FleetCredential {self.fleet_external_id} org={self.organization_id}"

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation(
            "OrganizationFleetCredential must not be hard-deleted"
        )


class FleetCredentialEvent(models.Model):
    """Append-only audit for fleet credential issue/rotate (ADR 021)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="fleet_credential_events",
    )
    fleet_credential = models.ForeignKey(
        OrganizationFleetCredential,
        on_delete=models.CASCADE,
        related_name="events",
    )
    action = models.CharField(max_length=32, choices=FleetCredentialEventAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fleet_credential_events",
    )
    fleet_external_id = models.CharField(max_length=64)
    previous_valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "fleet credential event"
        verbose_name_plural = "fleet credential events"
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="org_fcevt_org_created",
            ),
        ]

    def __str__(self) -> str:
        return f"FleetCredentialEvent {self.action} {self.fleet_external_id}"

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteViolation("FleetCredentialEvent must not be hard-deleted")
