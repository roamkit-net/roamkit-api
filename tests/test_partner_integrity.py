"""partner_integrity_check is diagnostic only."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Account
from apps.billing.partner_channel import (
    CustomerAttribution,
    PartnerChannel,
    PartnerInviteLink,
)
from apps.billing.services.partner_invite import (
    create_individual_partner_channel,
    create_partner_channel,
)
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

PASSWORD = "SecurePass1!"


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _team(actor: User) -> PartnerChannel:
    org = create_organization(name=f"Team {uuid.uuid4()}", actor=actor)
    return create_partner_channel(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
    )


@pytest.mark.django_db
def test_clean_individual_and_team_exit_zero():
    owner = _user("owner")
    create_individual_partner_channel(owner=owner)
    _team(_user("founder"))
    stdout = StringIO()
    call_command("partner_integrity_check", "--format=json", stdout=stdout)
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "clean"
    assert payload["count"] == 0


def _blank_link(channel: PartnerChannel, *, active: bool) -> None:
    PartnerInviteLink.objects.bulk_create(
        [
            PartnerInviteLink(
                partner_channel=channel,
                token="",
                is_active=active,
            )
        ]
    )


@pytest.mark.django_db
def test_blank_invite_token_fails_integrity_when_active():
    channel = _team(_user("founder"))
    _blank_link(channel, active=True)
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("partner_integrity_check", "--format=json", stdout=stdout)
    assert exc.value.code == 1
    payload = json.loads(stdout.getvalue())
    assert payload["issues"][0]["code"] == "invite_token_missing"


@pytest.mark.django_db
def test_blank_invite_token_fails_integrity_when_inactive():
    channel = _team(_user("founder"))
    _blank_link(channel, active=False)
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("partner_integrity_check", stdout=stdout)
    assert exc.value.code == 1
    assert "invite_token_missing" in stdout.getvalue()


@pytest.mark.django_db
def test_inactive_valid_invite_is_clean():
    channel = _team(_user("founder"))
    link = channel.invite_links.get()
    link.is_active = False
    link.save(update_fields=["is_active"])
    stdout = StringIO()
    call_command("partner_integrity_check", "--format=json", stdout=stdout)
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "clean"
    assert payload["count"] == 0


@pytest.mark.django_db
def test_illegal_self_attribution_is_reported_and_not_repaired():
    owner = _user("owner")
    channel = create_individual_partner_channel(owner=owner)
    CustomerAttribution.objects.create(
        user=owner,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("partner_integrity_check", stdout=stdout)
    assert exc.value.code == 1
    assert "individual_self_attribution" in stdout.getvalue()
    assert CustomerAttribution.objects.filter(
        user=owner, partner_channel=channel
    ).exists()
    owner.billing_account.refresh_from_db()
    assert owner.billing_account.balance == Decimal("0.000000")


@pytest.mark.django_db
def test_active_team_membership_conflict_is_reported():
    founder = _user("founder")
    channel = _team(founder)
    member = _user("member")
    Membership.objects.create(
        organization=channel.organization,
        user=member,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )
    CustomerAttribution.objects.create(
        user=member,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("partner_integrity_check", stdout=stdout)
    text = stdout.getvalue()
    assert exc.value.code == 1
    assert "team_self_attribution" in text
    assert "team_member_attributed" not in text
    assert text.count("team_self_attribution") == 1


@pytest.mark.django_db
def test_suspended_membership_is_not_a_conflict():
    founder = _user("founder")
    channel = _team(founder)
    member = _user("member")
    Membership.objects.create(
        organization=channel.organization,
        user=member,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.SUSPENDED,
    )
    CustomerAttribution.objects.create(
        user=member,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    call_command("partner_integrity_check")


@pytest.mark.django_db
def test_missing_personal_account_is_a_settlement_violation():
    owner = _user("owner")
    create_individual_partner_channel(owner=owner)
    Account.objects.filter(user=owner).delete()
    stdout = StringIO()
    with pytest.raises(SystemExit) as exc:
        call_command("partner_integrity_check", stdout=stdout)
    assert exc.value.code == 1
    assert "settlement_account_missing" in stdout.getvalue()


@pytest.mark.django_db
def test_team_conflict_scan_stays_set_based_as_customers_grow():
    founder = _user("founder")
    channel = _team(founder)
    for index in range(10):
        customer = _user(f"customer-{index}")
        CustomerAttribution.objects.create(
            user=customer,
            partner_channel=channel,
            source=CustomerAttribution.Source.ADMIN,
            attributed_at=timezone.now(),
        )
    Membership.objects.create(
        organization=channel.organization,
        user=CustomerAttribution.objects.filter(partner_channel=channel)
        .order_by("user_id")
        .first()
        .user,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
    )
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(SystemExit) as exc:
            call_command("partner_integrity_check")
    assert exc.value.code == 1
    membership_queries = [
        query for query in ctx.captured_queries if "membership" in query["sql"].lower()
    ]
    assert len(membership_queries) <= 2
