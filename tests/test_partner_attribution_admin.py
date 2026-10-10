"""Admin assign and transfer do not touch the ledger."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

from apps.billing.models import AccountKind, CreditLedgerEntry
from apps.billing.partner_channel import (
    CustomerAttribution,
    CustomerAttributionHistory,
)
from apps.billing.services.partner_attribution import (
    CustomerAttributionExists,
    assign_customer_attribution,
)
from apps.billing.services.partner_invite import (
    create_individual_partner_channel,
    create_partner_channel,
)
from apps.organizations.services.account_binding import create_organization

User = get_user_model()

ENABLED = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)


def _user(prefix: str):
    return User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password="SecurePass1!",
    )


def _staff():
    staff = _user("staff")
    staff.is_staff = True
    staff.is_superuser = True
    staff.save(update_fields=["is_staff", "is_superuser"])
    return staff


def _channel(actor):
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    return create_partner_channel(organization=org)


@ENABLED
@pytest.mark.django_db
def test_assign_writes_no_ledger_and_rejects_a_second_partner() -> None:
    staff = _staff()
    customer = _user("customer")
    channel = _channel(staff)
    ledger_before = CreditLedgerEntry.objects.count()
    balance_before = customer.billing_account.balance

    created = assign_customer_attribution(
        user=customer,
        partner_channel=channel,
        actor=staff,
    )

    assert created.source == CustomerAttribution.Source.ADMIN
    assert created.registered_via_invite is False
    assert created.bonus_amount_snapshot is None
    assert created.invite_visit_id is None
    assert created.invite_link_id is None
    assert CreditLedgerEntry.objects.count() == ledger_before
    assert CustomerAttributionHistory.objects.count() == 0
    customer.billing_account.refresh_from_db()
    assert customer.billing_account.balance == balance_before
    with pytest.raises(CustomerAttributionExists):
        assign_customer_attribution(
            user=customer,
            partner_channel=channel,
            actor=staff,
        )
    assert CustomerAttribution.objects.filter(user=customer).count() == 1


@ENABLED
@pytest.mark.django_db
def test_admin_assign_logs_the_actor_and_does_not_credit() -> None:
    staff = _staff()
    customer = _user("customer")
    channel = _channel(_user("owner"))
    client = Client()
    client.force_login(staff)
    ledger_before = CreditLedgerEntry.objects.count()

    response = client.post(
        "/admin/billing/customerattribution/add/",
        {"user": str(customer.pk), "partner_channel": str(channel.pk)},
    )

    assert response.status_code == 302
    attribution = CustomerAttribution.objects.get(user=customer)
    assert attribution.partner_channel_id == channel.pk
    assert attribution.source == CustomerAttribution.Source.ADMIN
    assert CreditLedgerEntry.objects.count() == ledger_before
    assert CustomerAttributionHistory.objects.count() == 0
    log = LogEntry.objects.get(object_id=str(attribution.pk), action_flag=ADDITION)
    assert log.user_id == staff.pk


@ENABLED
@pytest.mark.django_db
def test_admin_transfer_keeps_balances() -> None:
    staff = _staff()
    customer = _user("customer")
    first = _channel(_user("owner-a"))
    second = _channel(_user("owner-b"))
    attribution = assign_customer_attribution(
        user=customer,
        partner_channel=first,
        actor=staff,
    )
    customer.billing_account.refresh_from_db()
    first.organization.account.refresh_from_db()
    second.organization.account.refresh_from_db()
    customer_before = customer.billing_account.balance
    first_before = first.organization.account.balance
    second_before = second.organization.account.balance
    ledger_before = CreditLedgerEntry.objects.count()
    client = Client()
    client.force_login(staff)

    response = client.post(
        f"/admin/billing/customerattribution/{attribution.pk}/change/",
        {"partner_channel": str(second.pk)},
    )

    assert response.status_code == 302
    attribution.refresh_from_db()
    assert attribution.partner_channel_id == second.pk
    assert attribution.source == CustomerAttribution.Source.ADMIN
    assert CreditLedgerEntry.objects.count() == ledger_before
    assert CustomerAttributionHistory.objects.count() == 0
    customer.billing_account.refresh_from_db()
    first.organization.account.refresh_from_db()
    second.organization.account.refresh_from_db()
    assert customer.billing_account.balance == customer_before
    assert first.organization.account.balance == first_before
    assert second.organization.account.balance == second_before


@ENABLED
@pytest.mark.django_db
def test_admin_rejects_a_second_assign_and_self_referral() -> None:
    staff = _staff()
    customer = _user("customer")
    channel = _channel(_user("owner"))
    assign_customer_attribution(user=customer, partner_channel=channel, actor=staff)
    client = Client()
    client.force_login(staff)

    duplicate = client.post(
        "/admin/billing/customerattribution/add/",
        {"user": str(customer.pk), "partner_channel": str(channel.pk)},
    )

    assert duplicate.status_code == 200
    assert b"already has a partner" in duplicate.content
    assert CustomerAttribution.objects.filter(user=customer).count() == 1

    owner = _user("seller")
    individual = create_individual_partner_channel(owner=owner)
    refused = client.post(
        "/admin/billing/customerattribution/add/",
        {"user": str(owner.pk), "partner_channel": str(individual.pk)},
    )

    assert refused.status_code == 200
    assert b"cannot be attributed" in refused.content
    assert not CustomerAttribution.objects.filter(user=owner).exists()


@ENABLED
@pytest.mark.django_db
def test_organization_account_has_no_assign_link() -> None:
    staff = _staff()
    org = create_organization(name=f"Fleet {uuid.uuid4()}", actor=staff)
    customer = _user("customer")
    client = Client()
    client.force_login(staff)

    organization = client.get(f"/admin/billing/account/{org.account_id}/change/")
    personal = client.get(
        f"/admin/billing/account/{customer.billing_account.pk}/change/"
    )

    assert organization.status_code == 200
    assert b"not a customer attribution" in organization.content
    assert b"Assign partner" not in organization.content
    assert org.account.kind == AccountKind.ORGANIZATION
    assert personal.status_code == 200
    assert f"user={customer.pk}".encode() in personal.content
    assert b"Assign partner" in personal.content
