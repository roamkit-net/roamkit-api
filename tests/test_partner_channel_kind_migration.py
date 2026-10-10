"""Migration 0012 backfill, preflight, and conditional reverse (ADR 024)."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

LEAF = "0012_partner_channel_kind"
BEFORE = "0011_partner_invite_bonus"


def _migrate(executor: MigrationExecutor, billing: str | None) -> None:
    executor.loader.build_graph()
    executor.migrate([("billing", billing)])
    executor.loader.build_graph()


def _restore(executor: MigrationExecutor) -> None:
    executor.loader.build_graph()
    executor.migrate(executor.loader.graph.leaf_nodes())


def _apps(executor: MigrationExecutor):
    return executor.loader.project_state([("billing", BEFORE)]).apps


def _user(apps, email: str):
    User = apps.get_model("accounts", "User")
    return User.objects.create(
        email=email,
        password="!",
        is_active=True,
        is_staff=False,
        is_superuser=False,
    )


def _team_channel(apps, name: str):
    Account = apps.get_model("billing", "Account")
    Organization = apps.get_model("organizations", "Organization")
    PartnerChannel = apps.get_model("billing", "PartnerChannel")
    account = Account.objects.create(
        user=None,
        kind="organization",
        balance=Decimal("12.500000"),
        version=3,
    )
    organization = Organization.objects.create(
        name=name,
        status="active",
        account=account,
    )
    channel = PartnerChannel.objects.create(
        organization=organization,
        revenue_share_percent=Decimal("50.00"),
        is_active=True,
    )
    return account, organization, channel


@pytest.mark.django_db(transaction=True)
def test_existing_team_row_backfills_without_touching_money() -> None:
    executor = MigrationExecutor(connection)
    try:
        _migrate(executor, BEFORE)
        apps = _apps(executor)
        Ledger = apps.get_model("billing", "CreditLedgerEntry")
        Link = apps.get_model("billing", "PartnerInviteLink")
        Attribution = apps.get_model("billing", "CustomerAttribution")
        account, organization, channel = _team_channel(apps, "Legacy fleet")
        channel_id = channel.pk
        organization_id = organization.pk
        ledger = Ledger.objects.create(
            account=account,
            delta=Decimal("12.500000"),
            balance_after=Decimal("12.500000"),
            reference_type="deposit",
            reference_id=str(channel_id),
            idempotency_key=f"kind-backfill-{uuid.uuid4()}",
        )
        link = Link.objects.create(
            partner_channel=channel,
            token=f"legacy-{uuid.uuid4().hex}",
        )
        customer = _user(apps, f"legacy-customer-{uuid.uuid4().hex}@example.com")
        attribution = Attribution.objects.create(
            user=customer,
            partner_channel=channel,
            source="admin",
            attributed_at=timezone.now(),
        )
        ledger_count = Ledger.objects.count()

        _migrate(executor, LEAF)
        apps = executor.loader.project_state([("billing", LEAF)]).apps
        Channel = apps.get_model("billing", "PartnerChannel")
        Account = apps.get_model("billing", "Account")
        Ledger = apps.get_model("billing", "CreditLedgerEntry")
        Link = apps.get_model("billing", "PartnerInviteLink")
        Attribution = apps.get_model("billing", "CustomerAttribution")
        row = Channel.objects.get(pk=channel_id)
        assert row.kind == "team"
        assert row.owner_user_id is None
        assert row.organization_id == organization_id
        assert Account.objects.get(pk=account.pk).balance == Decimal("12.500000")
        assert Account.objects.get(pk=account.pk).kind == "organization"
        assert Ledger.objects.count() == ledger_count
        assert Ledger.objects.get(pk=ledger.pk).delta == Decimal("12.500000")
        assert Link.objects.get(pk=link.pk).token == link.token
        assert (
            Attribution.objects.get(pk=attribution.pk).partner_channel_id == channel_id
        )
    finally:
        _restore(executor)


@pytest.mark.django_db(transaction=True)
def test_active_member_attribution_blocks_migration_and_is_not_repaired() -> None:
    executor = MigrationExecutor(connection)
    attribution_id = None
    try:
        _migrate(executor, BEFORE)
        apps = _apps(executor)
        Membership = apps.get_model("organizations", "Membership")
        Attribution = apps.get_model("billing", "CustomerAttribution")
        _account, organization, channel = _team_channel(apps, "Conflict fleet")
        member = _user(apps, f"conflict-member-{uuid.uuid4().hex}@example.com")
        Membership.objects.create(
            organization=organization,
            user=member,
            role="viewer",
            status="active",
        )
        attribution = Attribution.objects.create(
            user=member,
            partner_channel=channel,
            source="admin",
            attributed_at=timezone.now(),
        )
        attribution_id = attribution.pk

        with pytest.raises(RuntimeError, match="TEAM self-attribution conflict"):
            _migrate(executor, LEAF)

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'billing_partnerchannel' AND column_name = 'kind'
                """
            )
            assert cursor.fetchone() is None
        apps = _apps(executor)
        Attribution = apps.get_model("billing", "CustomerAttribution")
        Membership = apps.get_model("organizations", "Membership")
        assert Attribution.objects.filter(pk=attribution_id).exists()
        membership = Membership.objects.get(user_id=member.pk)
        assert membership.status == "active"
        assert membership.role == "viewer"
    finally:
        if attribution_id is not None:
            executor.loader.build_graph()
            Attribution = _apps(executor).get_model("billing", "CustomerAttribution")
            Attribution.objects.filter(pk=attribution_id).delete()
        _restore(executor)


@pytest.mark.django_db(transaction=True)
def test_suspended_member_and_history_do_not_block_backfill() -> None:
    executor = MigrationExecutor(connection)
    try:
        _migrate(executor, BEFORE)
        apps = _apps(executor)
        Membership = apps.get_model("organizations", "Membership")
        Attribution = apps.get_model("billing", "CustomerAttribution")
        History = apps.get_model("billing", "CustomerAttributionHistory")
        _account, organization, channel = _team_channel(apps, "Suspended fleet")
        _other_account, other_org, other_channel = _team_channel(apps, "Other fleet")
        suspended = _user(apps, f"suspended-{uuid.uuid4().hex}@example.com")
        active = _user(apps, f"history-only-{uuid.uuid4().hex}@example.com")
        Membership.objects.create(
            organization=organization,
            user=suspended,
            role="admin",
            status="suspended",
        )
        Membership.objects.create(
            organization=organization,
            user=active,
            role="owner",
            status="active",
        )
        Attribution.objects.create(
            user=suspended,
            partner_channel=channel,
            source="admin",
            attributed_at=timezone.now(),
        )
        History.objects.create(
            user=active,
            from_partner_channel=channel,
            to_partner_channel=other_channel,
            changed_by_user_id_snapshot=active.pk,
            change_reason="moved",
            changed_at=timezone.now(),
            previous_attributed_at=timezone.now(),
        )
        _migrate(executor, LEAF)
        Channel = executor.loader.project_state([("billing", LEAF)]).apps.get_model(
            "billing", "PartnerChannel"
        )
        row = Channel.objects.get(pk=channel.pk)
        assert row.kind == "team"
        assert row.organization_id == organization.pk
        assert other_org.pk != organization.pk
    finally:
        _restore(executor)


@pytest.mark.django_db(transaction=True)
def test_reverse_refuses_individual_until_that_row_is_removed() -> None:
    executor = MigrationExecutor(connection)
    channel_id = None
    try:
        _migrate(executor, LEAF)
        apps = executor.loader.project_state([("billing", LEAF)]).apps
        owner = _user(apps, f"individual-{uuid.uuid4().hex}@example.com")
        Channel = apps.get_model("billing", "PartnerChannel")
        channel = Channel.objects.create(
            kind="individual",
            owner_user=owner,
            organization=None,
            revenue_share_percent=Decimal("15.00"),
            is_active=True,
        )
        channel_id = channel.pk
        with pytest.raises(RuntimeError, match="conditionally reversible"):
            _migrate(executor, BEFORE)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'billing_partnerchannel' AND column_name = 'kind'
                """
            )
            assert cursor.fetchone() is not None
        apps = executor.loader.project_state([("billing", LEAF)]).apps
        Channel = apps.get_model("billing", "PartnerChannel")
        Channel.objects.filter(pk=channel_id).delete()
        channel_id = None
        _migrate(executor, BEFORE)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'billing_partnerchannel' AND column_name = 'kind'
                """
            )
            assert cursor.fetchone() is None
    finally:
        if channel_id is not None:
            executor.loader.build_graph()
            apps = executor.loader.project_state([("billing", LEAF)]).apps
            Channel = apps.get_model("billing", "PartnerChannel")
            Channel.objects.filter(pk=channel_id).delete()
        _restore(executor)


@pytest.mark.django_db(transaction=True)
def test_legacy_team_round_trips_without_changing_money_or_rows() -> None:
    executor = MigrationExecutor(connection)
    try:
        _migrate(executor, BEFORE)
        apps = _apps(executor)
        Membership = apps.get_model("organizations", "Membership")
        Ledger = apps.get_model("billing", "CreditLedgerEntry")
        Link = apps.get_model("billing", "PartnerInviteLink")
        Attribution = apps.get_model("billing", "CustomerAttribution")
        account, organization, channel = _team_channel(apps, "Round trip fleet")
        owner = _user(apps, f"round-owner-{uuid.uuid4().hex}@example.com")
        customer = _user(apps, f"round-customer-{uuid.uuid4().hex}@example.com")
        Membership.objects.create(
            organization=organization,
            user=owner,
            role="owner",
            status="active",
        )
        ledger = Ledger.objects.create(
            account=account,
            delta=Decimal("12.500000"),
            balance_after=Decimal("12.500000"),
            reference_type="deposit",
            reference_id=str(channel.pk),
            idempotency_key=f"round-{uuid.uuid4()}",
        )
        link = Link.objects.create(
            partner_channel=channel,
            token=f"round-{uuid.uuid4().hex}",
        )
        attribution = Attribution.objects.create(
            user=customer,
            partner_channel=channel,
            source="admin",
            attributed_at=timezone.now(),
        )
        channel_id = channel.pk
        organization_id = organization.pk
        token = link.token

        _migrate(executor, LEAF)
        _migrate(executor, BEFORE)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'billing_partnerchannel' AND column_name = 'kind'
                """
            )
            assert cursor.fetchone() is None
        apps = _apps(executor)
        Channel = apps.get_model("billing", "PartnerChannel")
        Account = apps.get_model("billing", "Account")
        row = Channel.objects.get(pk=channel_id)
        assert row.organization_id == organization_id
        assert Account.objects.get(pk=account.pk).balance == Decimal("12.500000")
        assert Account.objects.get(pk=account.pk).kind == "organization"
        restored_ledger = apps.get_model("billing", "CreditLedgerEntry").objects.get(
            pk=ledger.pk
        )
        assert restored_ledger.delta == Decimal("12.500000")
        restored_link = apps.get_model("billing", "PartnerInviteLink").objects.get(
            pk=link.pk
        )
        assert restored_link.token == token
        restored_attribution = apps.get_model(
            "billing", "CustomerAttribution"
        ).objects.get(pk=attribution.pk)
        assert restored_attribution.partner_channel_id == channel_id
        restored_membership = apps.get_model("organizations", "Membership").objects.get(
            user_id=owner.pk
        )
        assert restored_membership.status == "active"

        _migrate(executor, LEAF)
        Channel = executor.loader.project_state([("billing", LEAF)]).apps.get_model(
            "billing", "PartnerChannel"
        )
        row = Channel.objects.get(pk=channel_id)
        assert row.kind == "team"
        assert row.owner_user_id is None
        assert row.organization_id == organization_id
    finally:
        _restore(executor)
