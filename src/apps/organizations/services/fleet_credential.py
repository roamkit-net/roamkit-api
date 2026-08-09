"""Organization fleet credentials (ADR 021 Option C′ foundation).

Plaintext secret is returned only on issue/rotate. Verification accepts the
current hash, or the previous hash until ``previous_valid_until``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.organizations.exceptions import (
    FleetCredentialConflictError,
    FleetCredentialInvalidError,
)
from apps.organizations.models import (
    FleetCredentialEvent,
    FleetCredentialEventAction,
    OrganizationFleetCredential,
)

if TYPE_CHECKING:
    from apps.accounts.models import User
    from apps.organizations.models import Organization


def _hash_credential(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_credential() -> str:
    return secrets.token_urlsafe(32)


def _new_fleet_external_id() -> str:
    """Opaque fleet lookup id — never Organization.id."""
    return secrets.token_urlsafe(24)


def _grace_seconds(override: int | None) -> int:
    if override is not None:
        if override < 0:
            raise ValueError("grace_seconds must be >= 0")
        return override
    return int(getattr(settings, "FLEET_CREDENTIAL_GRACE_SECONDS", 72 * 60 * 60))


def _record_event(
    *,
    fleet: OrganizationFleetCredential,
    action: str,
    actor: User | None,
) -> FleetCredentialEvent:
    return FleetCredentialEvent.objects.create(
        organization=fleet.organization,
        fleet_credential=fleet,
        action=action,
        actor=actor,
        fleet_external_id=fleet.fleet_external_id,
        previous_valid_until=fleet.previous_valid_until,
    )


@dataclass(frozen=True, slots=True)
class FleetCredentialIssueResult:
    fleet: OrganizationFleetCredential
    fleet_external_id: str
    credential: str


@transaction.atomic
def issue_fleet_credential(
    organization: Organization,
    *,
    actor: User | None = None,
    grace_seconds: int | None = None,
) -> FleetCredentialIssueResult:
    """Create the org's fleet credential row and return plaintext once.

    ``grace_seconds`` is accepted for API symmetry with rotate but unused on
    first issue (there is no previous secret).
    """
    _ = _grace_seconds(grace_seconds)
    if OrganizationFleetCredential.objects.filter(organization=organization).exists():
        raise FleetCredentialConflictError(
            "Organization already has a fleet credential; use rotate"
        )

    raw = _new_credential()
    now = timezone.now()
    fleet = OrganizationFleetCredential.objects.create(
        organization=organization,
        fleet_external_id=_new_fleet_external_id(),
        current_credential_hash=_hash_credential(raw),
        current_issued_at=now,
        previous_credential_hash="",
        previous_valid_until=None,
    )
    if fleet.fleet_external_id == str(organization.id):
        # Defensive: opaque generator must never collide with org PK string.
        raise FleetCredentialConflictError("fleet_external_id must not equal org id")

    _record_event(
        fleet=fleet,
        action=FleetCredentialEventAction.ISSUE,
        actor=actor,
    )
    return FleetCredentialIssueResult(
        fleet=fleet,
        fleet_external_id=fleet.fleet_external_id,
        credential=raw,
    )


@transaction.atomic
def rotate_fleet_credential(
    organization: Organization,
    *,
    actor: User | None = None,
    grace_seconds: int | None = None,
) -> FleetCredentialIssueResult:
    """Issue a new current secret; previous remains valid for the grace window."""
    fleet = (
        OrganizationFleetCredential.objects.select_for_update()
        .filter(organization=organization)
        .first()
    )
    if fleet is None:
        raise FleetCredentialInvalidError("Organization has no fleet credential")

    grace = _grace_seconds(grace_seconds)
    raw = _new_credential()
    now = timezone.now()
    fleet.previous_credential_hash = fleet.current_credential_hash
    fleet.previous_valid_until = now + timedelta(seconds=grace) if grace > 0 else now
    fleet.current_credential_hash = _hash_credential(raw)
    fleet.current_issued_at = now
    fleet.save(
        update_fields=[
            "previous_credential_hash",
            "previous_valid_until",
            "current_credential_hash",
            "current_issued_at",
            "updated_at",
        ]
    )
    _record_event(
        fleet=fleet,
        action=FleetCredentialEventAction.ROTATE,
        actor=actor,
    )
    return FleetCredentialIssueResult(
        fleet=fleet,
        fleet_external_id=fleet.fleet_external_id,
        credential=raw,
    )


def verify_fleet_credential(
    fleet_external_id: str,
    credential: str,
) -> OrganizationFleetCredential:
    """Return fleet row if current (or in-grace previous) secret matches."""
    external_id = (fleet_external_id or "").strip()
    raw = credential or ""
    if not external_id or not raw:
        raise FleetCredentialInvalidError("Fleet credential invalid")

    fleet = OrganizationFleetCredential.objects.filter(
        fleet_external_id=external_id
    ).first()
    if fleet is None:
        raise FleetCredentialInvalidError("Fleet credential invalid")

    digest = _hash_credential(raw)
    if hmac.compare_digest(fleet.current_credential_hash, digest):
        return fleet

    previous = fleet.previous_credential_hash or ""
    until = fleet.previous_valid_until
    if (
        previous
        and until is not None
        and timezone.now() <= until
        and hmac.compare_digest(previous, digest)
    ):
        return fleet

    raise FleetCredentialInvalidError("Fleet credential invalid")
