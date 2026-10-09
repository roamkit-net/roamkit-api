"""Explicit CustomerAttribution transfer. Consume does not call this."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import CustomerAttribution, InviteVisit
from apps.billing.services.partner_attribution import (
    consume_partner_pending,
    credit_registration_invite_bonus,
    transfer_customer_attribution,
)
from apps.billing.services.partner_invite import (
    canonical_invite_link,
    create_partner_channel,
    issue_join_signature,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)
PASSWORD = "SecurePass1!"


def _user(prefix: str) -> User:
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )


def _channel(actor: User):
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    return create_partner_channel(organization=org)


def _invite_attribution(channel, user: User) -> CustomerAttribution:
    link = canonical_invite_link(channel)
    link.name = "October"
    link.source = "tiktok"
    link.campaign = "fall"
    link.content = "video"
    link.bonus_amount = Decimal("10.000000")
    link.save(
        update_fields=[
            "name",
            "source",
            "campaign",
            "content",
            "bonus_amount",
            "updated_at",
        ]
    )
    visit = InviteVisit.objects.create(
        invite_link=link,
        utm_source="tiktok",
        utm_medium="social",
        utm_campaign="fall-utm",
        utm_content="bio",
    )
    return CustomerAttribution.objects.create(
        user=user,
        partner_channel=channel,
        source=CustomerAttribution.Source.INVITE_LINK,
        invite_link=link,
        invite_visit=visit,
        invite_token=link.token,
        registered_via_invite=True,
        invite_name_snapshot=link.name,
        invite_source_snapshot=link.source,
        invite_campaign_snapshot=link.campaign,
        invite_content_snapshot=link.content,
        utm_source_snapshot=visit.utm_source,
        utm_medium_snapshot=visit.utm_medium,
        utm_campaign_snapshot=visit.utm_campaign,
        utm_content_snapshot=visit.utm_content,
        bonus_amount_snapshot=Decimal("10.000000"),
        attributed_at=timezone.now() - timedelta(days=1),
    )


@ENABLED
@pytest.mark.django_db
def test_transfer_changes_only_the_channel_and_does_not_credit() -> None:
    owner_a = _user("owner-a")
    owner_b = _user("owner-b")
    customer = _user("customer")
    team_a = _channel(owner_a)
    team_b = _channel(owner_b)
    attribution = _invite_attribution(team_a, customer)
    credit_registration_invite_bonus(attribution=attribution)
    customer.billing_account.refresh_from_db()
    before = {
        "invite_visit_id": attribution.invite_visit_id,
        "invite_link_id": attribution.invite_link_id,
        "invite_token": attribution.invite_token,
        "registered_via_invite": attribution.registered_via_invite,
        "invite_name_snapshot": attribution.invite_name_snapshot,
        "invite_source_snapshot": attribution.invite_source_snapshot,
        "invite_campaign_snapshot": attribution.invite_campaign_snapshot,
        "invite_content_snapshot": attribution.invite_content_snapshot,
        "utm_source_snapshot": attribution.utm_source_snapshot,
        "utm_medium_snapshot": attribution.utm_medium_snapshot,
        "utm_campaign_snapshot": attribution.utm_campaign_snapshot,
        "utm_content_snapshot": attribution.utm_content_snapshot,
        "bonus_amount_snapshot": attribution.bonus_amount_snapshot,
        "source": attribution.source,
        "attributed_at": attribution.attributed_at,
        "created_at": attribution.created_at,
    }
    ledger_before = CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).count()
    user_before = customer.billing_account.balance
    team_a_before = team_a.organization.account.balance
    team_b_before = team_b.organization.account.balance

    moved = transfer_customer_attribution(user=customer, partner_channel=team_b)

    moved.refresh_from_db()
    assert moved.pk == attribution.pk
    assert moved.partner_channel_id == team_b.pk
    for name, value in before.items():
        assert getattr(moved, name) == value
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
        ).count()
        == ledger_before
    )
    customer.billing_account.refresh_from_db()
    team_a.organization.account.refresh_from_db()
    team_b.organization.account.refresh_from_db()
    assert customer.billing_account.balance == user_before
    assert team_a.organization.account.balance == team_a_before
    assert team_b.organization.account.balance == team_b_before


@ENABLED
@pytest.mark.django_db
def test_transfer_to_the_same_channel_does_not_write() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    attribution = _invite_attribution(channel, customer)
    with CaptureQueriesContext(connection) as captured:
        returned = transfer_customer_attribution(user=customer, partner_channel=channel)
    assert returned.pk == attribution.pk
    assert returned.partner_channel_id == channel.pk
    assert not any(
        query["sql"].lstrip().upper().startswith("UPDATE")
        for query in captured.captured_queries
    )


@pytest.mark.django_db
def test_transfer_without_attribution_raises() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    with pytest.raises(CustomerAttribution.DoesNotExist):
        transfer_customer_attribution(user=customer, partner_channel=channel)
    assert not CustomerAttribution.objects.filter(user=customer).exists()


@pytest.mark.django_db
def test_admin_transfer_keeps_admin_semantics() -> None:
    owner_a = _user("owner-a")
    owner_b = _user("owner-b")
    customer = _user("customer")
    team_a = _channel(owner_a)
    team_b = _channel(owner_b)
    CustomerAttribution.objects.create(
        user=customer,
        partner_channel=team_a,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    moved = transfer_customer_attribution(user=customer, partner_channel=team_b)
    moved.refresh_from_db()
    assert moved.partner_channel_id == team_b.pk
    assert moved.source == CustomerAttribution.Source.ADMIN
    assert moved.invite_visit_id is None
    assert moved.invite_link_id is None
    assert moved.invite_token is None
    assert moved.registered_via_invite is False
    assert moved.bonus_amount_snapshot is None
    assert moved.invite_source_snapshot == ""
    assert moved.utm_source_snapshot == ""
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@ENABLED
@pytest.mark.django_db
def test_consume_of_another_team_does_not_transfer() -> None:
    owner_a = _user("owner-a")
    owner_b = _user("owner-b")
    customer = _user("customer")
    team_a = _channel(owner_a)
    team_b = _channel(owner_b)
    attribution = _invite_attribution(team_a, customer)
    visit_id = attribution.invite_visit_id
    signed = issue_join_signature(canonical_invite_link(team_b).token)
    assert consume_partner_pending(customer, signed) == "noop"
    attribution.refresh_from_db()
    assert attribution.partner_channel_id == team_a.pk
    assert attribution.invite_visit_id == visit_id
    assert attribution.registered_via_invite is True
    assert attribution.bonus_amount_snapshot == Decimal("10.000000")


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_transfers_wait_on_the_attribution_row_lock() -> None:
    """PostgreSQL row lock: the second transfer waits on the same attribution.

    A backend without row locks would not make the second call wait.
    """
    owner_a = _user("owner-a")
    owner_b = _user("owner-b")
    customer = _user("customer")
    team_a = _channel(owner_a)
    team_b = _channel(owner_b)
    attribution = _invite_attribution(team_a, customer)
    locked = threading.Event()
    release = threading.Event()
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def hold() -> None:
        try:
            connection.close()
            with transaction.atomic():
                CustomerAttribution.objects.select_for_update().get(pk=attribution.pk)
                locked.set()
                release.wait(timeout=5)
        except BaseException as exc:
            errors.append(exc)
            locked.set()
        finally:
            connection.close()

    def move() -> None:
        try:
            connection.close()
            locked.wait(timeout=5)
            started = time.monotonic()
            moved = transfer_customer_attribution(user=customer, partner_channel=team_b)
            result["elapsed"] = time.monotonic() - started
            result["channel_id"] = moved.partner_channel_id
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    holder = threading.Thread(target=hold)
    holder.start()
    assert locked.wait(timeout=5)
    mover = threading.Thread(target=move)
    mover.start()
    time.sleep(0.4)
    release.set()
    holder.join(timeout=5)
    mover.join(timeout=5)

    assert errors == []
    assert result["elapsed"] >= 0.3
    assert result["channel_id"] == team_b.pk
    attribution.refresh_from_db()
    assert attribution.partner_channel_id == team_b.pk
    assert attribution.invite_token is not None
