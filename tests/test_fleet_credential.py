"""Organization fleet credential foundation (ADR 021 Option C′)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.organizations.exceptions import (
    FleetCredentialConflictError,
    FleetCredentialInvalidError,
)
from apps.organizations.models import (
    FleetCredentialEvent,
    FleetCredentialEventAction,
    HardDeleteViolation,
    OrganizationFleetCredential,
)
from apps.organizations.services import (
    create_organization,
    issue_fleet_credential,
    rotate_fleet_credential,
    verify_fleet_credential,
)

User = get_user_model()


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="fleet-owner@example.com", password="x")


@pytest.fixture
def organization(owner):
    return create_organization(name="Serial Fleet", actor=owner)


@pytest.mark.django_db
def test_issue_fleet_credential_opaque_id_and_hash_at_rest(organization, owner):
    result = issue_fleet_credential(organization, actor=owner)

    assert result.fleet_external_id
    assert result.fleet_external_id != str(organization.id)
    assert result.credential
    assert result.credential not in result.fleet.current_credential_hash
    assert result.fleet.previous_credential_hash == ""
    assert result.fleet.previous_valid_until is None

    event = FleetCredentialEvent.objects.get(organization=organization)
    assert event.action == FleetCredentialEventAction.ISSUE
    assert event.fleet_external_id == result.fleet_external_id
    assert event.actor_id == owner.id


@pytest.mark.django_db
def test_issue_fleet_credential_twice_conflicts(organization, owner):
    issue_fleet_credential(organization, actor=owner)
    with pytest.raises(FleetCredentialConflictError):
        issue_fleet_credential(organization, actor=owner)


@pytest.mark.django_db
def test_verify_accepts_current_secret(organization, owner):
    issued = issue_fleet_credential(organization, actor=owner)
    fleet = verify_fleet_credential(issued.fleet_external_id, issued.credential)
    assert fleet.organization_id == organization.id


@pytest.mark.django_db
def test_rotate_keeps_previous_during_grace(organization, owner):
    issued = issue_fleet_credential(organization, actor=owner)
    old_secret = issued.credential
    rotated = rotate_fleet_credential(organization, actor=owner, grace_seconds=3600)

    assert rotated.credential != old_secret
    assert (
        verify_fleet_credential(issued.fleet_external_id, rotated.credential).pk
        == issued.fleet.pk
    )
    assert (
        verify_fleet_credential(issued.fleet_external_id, old_secret).pk
        == issued.fleet.pk
    )

    event = (
        FleetCredentialEvent.objects.filter(action=FleetCredentialEventAction.ROTATE)
        .order_by("-created_at")
        .first()
    )
    assert event is not None
    assert event.previous_valid_until is not None


@pytest.mark.django_db
def test_rotate_rejects_previous_after_grace(organization, owner):
    issued = issue_fleet_credential(organization, actor=owner)
    old_secret = issued.credential
    rotate_fleet_credential(organization, actor=owner, grace_seconds=60)

    fleet = OrganizationFleetCredential.objects.get(organization=organization)
    fleet.previous_valid_until = timezone.now() - timedelta(seconds=1)
    fleet.save(update_fields=["previous_valid_until", "updated_at"])

    with pytest.raises(FleetCredentialInvalidError):
        verify_fleet_credential(issued.fleet_external_id, old_secret)


@pytest.mark.django_db
def test_verify_rejects_wrong_secret_and_unknown_id(organization, owner):
    issued = issue_fleet_credential(organization, actor=owner)
    with pytest.raises(FleetCredentialInvalidError):
        verify_fleet_credential(issued.fleet_external_id, "not-the-secret")
    with pytest.raises(FleetCredentialInvalidError):
        verify_fleet_credential("missing-fleet-id", issued.credential)


@pytest.mark.django_db
def test_fleet_credential_hard_delete_blocked(organization, owner):
    issued = issue_fleet_credential(organization, actor=owner)
    with pytest.raises(HardDeleteViolation):
        issued.fleet.delete()
    with pytest.raises(HardDeleteViolation):
        OrganizationFleetCredential.objects.filter(pk=issued.fleet.pk).delete()
