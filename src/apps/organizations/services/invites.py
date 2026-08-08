"""Organization invite create / revoke / accept (ADR 020).

Accept never merges personal wallets and never moves eSIM inventory.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound

from apps.organizations.exceptions import (
    InviteConflictError,
    InviteInvalidError,
    NotAllowedError,
)
from apps.organizations.models import (
    InviteRole,
    InviteStatus,
    Membership,
    MembershipStatus,
    OrganizationInvite,
    OrganizationStatus,
)
from apps.organizations.services.authz import require_invite
from apps.organizations.services.context import resolve_organization_context

if TYPE_CHECKING:
    from apps.accounts.models import User


def normalize_invite_email(email: str) -> str:
    return email.strip().lower()


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_raw_token() -> str:
    return secrets.token_urlsafe(32)


def _invite_ttl() -> timedelta:
    seconds = int(getattr(settings, "ORGANIZATION_INVITE_TTL_SECONDS", 7 * 24 * 3600))
    return timedelta(seconds=seconds)


@dataclass(frozen=True, slots=True)
class InviteCreateResult:
    invite: OrganizationInvite
    raw_token: str
    created: bool


@transaction.atomic
def create_invite(
    *,
    actor: User,
    organization_id,
    email: str,
    role: str = InviteRole.MEMBER,
) -> InviteCreateResult:
    """Create or refresh a pending invite for ``(org, normalized email)``.

    At most one pending invite per org+email (DB). A second create updates
    role/expiry and rotates the token (returns the existing row).
    """
    if role not in {InviteRole.ADMIN, InviteRole.MEMBER, InviteRole.VIEWER}:
        raise NotAllowedError("Invalid invite role.")

    ctx = resolve_organization_context(actor, organization_id)
    require_invite(ctx)
    org = ctx.organization
    assert org is not None
    if org.status != OrganizationStatus.ACTIVE:
        raise NotAllowedError("Organization is not active.")

    email_normalized = normalize_invite_email(email)
    if not email_normalized:
        raise NotAllowedError("Email is required.")

    # Already an active member?
    existing_member = (
        Membership.objects.select_related("user")
        .filter(
            organization=org,
            status=MembershipStatus.ACTIVE,
            user__email__iexact=email_normalized,
        )
        .first()
    )
    if existing_member is not None:
        raise InviteConflictError("User is already an active organization member.")

    now = timezone.now()
    expires_at = now + _invite_ttl()
    raw_token = _new_raw_token()
    token_hash = _hash_token(raw_token)

    pending = (
        OrganizationInvite.objects.select_for_update()
        .filter(
            organization=org,
            email_normalized=email_normalized,
            status=InviteStatus.PENDING,
        )
        .first()
    )
    if pending is not None:
        pending.email = email.strip()
        pending.role = role
        pending.token_hash = token_hash
        pending.expires_at = expires_at
        pending.invited_by = actor
        pending.save(
            update_fields=[
                "email",
                "role",
                "token_hash",
                "expires_at",
                "invited_by",
                "updated_at",
            ]
        )
        return InviteCreateResult(invite=pending, raw_token=raw_token, created=False)

    invite = OrganizationInvite.objects.create(
        organization=org,
        email=email.strip(),
        email_normalized=email_normalized,
        role=role,
        status=InviteStatus.PENDING,
        token_hash=token_hash,
        expires_at=expires_at,
        invited_by=actor,
    )
    return InviteCreateResult(invite=invite, raw_token=raw_token, created=True)


@transaction.atomic
def revoke_invite(*, actor: User, organization_id, invite_id) -> OrganizationInvite:
    ctx = resolve_organization_context(actor, organization_id)
    require_invite(ctx)
    org = ctx.organization
    assert org is not None

    invite = (
        OrganizationInvite.objects.select_for_update()
        .filter(pk=invite_id, organization=org)
        .first()
    )
    if invite is None:
        raise NotFound(detail="Invite not found.")
    if invite.status != InviteStatus.PENDING:
        return invite

    invite.status = InviteStatus.REVOKED
    invite.revoked_at = timezone.now()
    invite.save(update_fields=["status", "revoked_at", "updated_at"])
    return invite


@dataclass(frozen=True, slots=True)
class InviteAcceptResult:
    membership: Membership
    invite: OrganizationInvite
    already_accepted: bool


def accept_invite(*, actor: User, raw_token: str) -> InviteAcceptResult:
    """Accept invite by token. Single-use under ``select_for_update``.

    Does not merge Accounts or move eSIM inventory.
    """
    if not raw_token or not raw_token.strip():
        raise InviteInvalidError("Invalid invite token.")

    token_hash = _hash_token(raw_token.strip())

    with transaction.atomic():
        invite = (
            OrganizationInvite.objects.select_for_update()
            .select_related("organization")
            .filter(token_hash=token_hash)
            .first()
        )
        if invite is None:
            raise InviteInvalidError("Invalid invite token.")

        now = timezone.now()

        # Idempotent replay by the same acceptor.
        if invite.status == InviteStatus.ACCEPTED:
            if invite.accepted_by_id == actor.pk:
                membership = Membership.objects.get(
                    organization_id=invite.organization_id,
                    user=actor,
                )
                return InviteAcceptResult(
                    membership=membership,
                    invite=invite,
                    already_accepted=True,
                )
            raise InviteInvalidError("Invite has already been accepted.")

        if invite.status == InviteStatus.REVOKED:
            raise InviteInvalidError("Invite has been revoked.")

        if invite.status == InviteStatus.EXPIRED or invite.expires_at <= now:
            # Persist EXPIRED before leaving the atomic block so reject does
            # not roll back the status transition.
            if invite.status == InviteStatus.PENDING:
                invite.status = InviteStatus.EXPIRED
                invite.save(update_fields=["status", "updated_at"])
            expired = True
        else:
            expired = False

        if not expired:
            if invite.status != InviteStatus.PENDING:
                raise InviteInvalidError("Invite is not pending.")

            if invite.organization.status != OrganizationStatus.ACTIVE:
                raise InviteInvalidError("Organization is not active.")

            if normalize_invite_email(actor.email) != invite.email_normalized:
                raise InviteInvalidError(
                    "Invite email does not match authenticated user."
                )

            membership, _created = Membership.objects.get_or_create(
                organization_id=invite.organization_id,
                user=actor,
                defaults={
                    "role": invite.role,
                    "status": MembershipStatus.ACTIVE,
                },
            )
            if (
                membership.status != MembershipStatus.ACTIVE
                or membership.role != invite.role
            ):
                membership.status = MembershipStatus.ACTIVE
                membership.role = invite.role
                membership.save(update_fields=["status", "role", "updated_at"])

            invite.status = InviteStatus.ACCEPTED
            invite.accepted_by = actor
            invite.accepted_at = now
            invite.save(
                update_fields=["status", "accepted_by", "accepted_at", "updated_at"]
            )

            return InviteAcceptResult(
                membership=membership,
                invite=invite,
                already_accepted=False,
            )

    raise InviteInvalidError("Invite has expired.")


def list_pending_invites(*, actor: User, organization_id):
    ctx = resolve_organization_context(actor, organization_id)
    require_invite(ctx)
    assert ctx.organization is not None
    return OrganizationInvite.objects.filter(
        organization=ctx.organization,
        status=InviteStatus.PENDING,
    ).order_by("-created_at")
