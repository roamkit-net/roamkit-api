"""Write-time self-referral (ADR 024). Illegal pairs are refused, not repaired."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_attribution import (
    apply_pending_on_activation,
    transfer_customer_attribution,
)
from apps.billing.services.partner_invite import (
    create_individual_partner_channel,
    create_partner_channel,
)
from apps.billing.services.partner_self_referral import PartnerSelfReferralConflict
from apps.organizations.exceptions import InviteConflictError
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization
from apps.organizations.services.invites import accept_invite, create_invite

PASSWORD = "SecurePass1!"


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _team(actor: User):
    org = create_organization(name=f"Team {uuid.uuid4()}", actor=actor)
    return create_partner_channel(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
    )


def _attribute(user: User, channel: PartnerChannel) -> CustomerAttribution:
    return CustomerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )


@pytest.mark.django_db
def test_individual_owner_cannot_be_attributed_to_own_channel():
    owner = _user("owner")
    other = _team(_user("founder"))
    own = create_individual_partner_channel(owner=owner)
    _attribute(owner, other)
    before = CreditLedgerEntry.objects.count()

    with pytest.raises(PartnerSelfReferralConflict):
        transfer_customer_attribution(user=owner, partner_channel=own)

    assert CustomerAttribution.objects.get(user=owner).partner_channel_id == other.pk
    assert CreditLedgerEntry.objects.count() == before


@pytest.mark.django_db
def test_active_team_member_cannot_be_attributed_to_that_channel():
    founder = _user("founder")
    channel = _team(founder)
    elsewhere = _team(_user("other-founder"))
    member = Membership.objects.get(organization=channel.organization)
    _attribute(member.user, elsewhere)

    with pytest.raises(PartnerSelfReferralConflict):
        transfer_customer_attribution(user=member.user, partner_channel=channel)

    assert (
        CustomerAttribution.objects.get(user=member.user).partner_channel_id
        == elsewhere.pk
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "role",
    [
        MembershipRole.ADMIN,
        MembershipRole.VIEWER,
        MembershipRole.MEMBER,
    ],
)
def test_every_active_role_blocks_team_attribution(role):
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    elsewhere = _team(_user("other-founder"))
    _attribute(customer, elsewhere)
    Membership.objects.create(
        organization=channel.organization,
        user=customer,
        role=role,
        status=MembershipStatus.ACTIVE,
    )

    with pytest.raises(PartnerSelfReferralConflict):
        transfer_customer_attribution(user=customer, partner_channel=channel)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [MembershipStatus.SUSPENDED, MembershipStatus.REVOKED],
)
def test_inactive_membership_does_not_block_attribution(status):
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    elsewhere = _team(_user("other-founder"))
    _attribute(customer, elsewhere)
    Membership.objects.create(
        organization=channel.organization,
        user=customer,
        role=MembershipRole.MEMBER,
        status=status,
    )

    moved = transfer_customer_attribution(user=customer, partner_channel=channel)

    assert moved.partner_channel_id == channel.pk


@pytest.mark.django_db
def test_membership_in_another_organization_does_not_block_attribution():
    channel = _team(_user("founder"))
    other = _team(_user("other-founder"))
    customer = Membership.objects.get(organization=other.organization).user
    elsewhere = _team(_user("third"))
    _attribute(customer, elsewhere)

    moved = transfer_customer_attribution(user=customer, partner_channel=channel)

    assert moved.partner_channel_id == channel.pk


@pytest.mark.django_db
def test_attributed_customer_cannot_accept_team_invite():
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    created = create_invite(
        actor=founder,
        organization_id=channel.organization_id,
        email=customer.email,
    )
    _attribute(customer, channel)

    with pytest.raises(InviteConflictError):
        accept_invite(actor=customer, raw_token=created.raw_token)

    assert not Membership.objects.filter(
        organization=channel.organization,
        user=customer,
    ).exists()
    assert (
        CustomerAttribution.objects.get(user=customer).partner_channel_id == channel.pk
    )


@pytest.mark.django_db
def test_invite_for_existing_attributed_user_is_refused():
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    _attribute(customer, channel)

    with pytest.raises(InviteConflictError):
        create_invite(
            actor=founder,
            organization_id=channel.organization_id,
            email=customer.email,
        )


@pytest.mark.django_db(transaction=True)
def test_attribute_and_activate_membership_cannot_both_succeed():
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    elsewhere = _team(_user("other-founder"))
    _attribute(customer, elsewhere)
    created = create_invite(
        actor=founder,
        organization_id=channel.organization_id,
        email=customer.email,
    )
    raw = created.raw_token

    def _attribute_to_team() -> str:
        try:
            transfer_customer_attribution(user=customer, partner_channel=channel)
            return "attributed"
        except PartnerSelfReferralConflict:
            return "conflict"
        finally:
            connection.close()

    def _activate() -> str:
        try:
            accept_invite(actor=customer, raw_token=raw)
            return "member"
        except InviteConflictError:
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = {
            pool.submit(_attribute_to_team).result(timeout=30),
            pool.submit(_activate).result(timeout=30),
        }

    assert results == {"attributed", "conflict"} or results == {"member", "conflict"}
    active = Membership.objects.filter(
        organization=channel.organization,
        user=customer,
        status=MembershipStatus.ACTIVE,
    ).exists()
    attributed = CustomerAttribution.objects.filter(
        user=customer,
        partner_channel=channel,
    ).exists()
    assert not (active and attributed)


@pytest.mark.django_db(transaction=True)
def test_concurrent_owner_attribution_does_not_land():
    owner = _user("owner")
    own = create_individual_partner_channel(owner=owner)
    elsewhere = _team(_user("founder"))
    _attribute(owner, elsewhere)

    def _move() -> str:
        try:
            transfer_customer_attribution(user=owner, partner_channel=own)
            return "attributed"
        except PartnerSelfReferralConflict:
            return "conflict"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(_move).result(timeout=30) for _ in range(2)]

    assert results == ["conflict", "conflict"]
    assert (
        CustomerAttribution.objects.get(user=owner).partner_channel_id == elsewhere.pk
    )


@pytest.mark.django_db
def test_pending_self_referral_keeps_activation_and_the_pending_row():
    founder = _user("founder")
    channel = _team(founder)
    customer = _user("customer")
    Membership.objects.create(
        organization=channel.organization,
        user=customer,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )
    link = PartnerInviteLink.objects.get(partner_channel=channel)
    PendingPartnerAttribution.objects.create(
        user=customer,
        partner_channel=channel,
        invite_token_snapshot=link.token,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    with transaction.atomic():
        customer.is_active = True
        customer.save(update_fields=["is_active", "updated_at"])
        apply_pending_on_activation(customer)

    customer.refresh_from_db()
    assert customer.is_active is True
    assert PendingPartnerAttribution.objects.filter(user=customer).exists()
    assert not CustomerAttribution.objects.filter(user=customer).exists()
    assert CreditLedgerEntry.objects.filter(account__user=customer).count() == 0
