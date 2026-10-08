"""partner_channel_reconcile reports drift and does not repair it."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.accounts.models import User
from apps.billing.models import LedgerReferenceType
from apps.billing.partner_channel import PartnerChannel
from apps.billing.services.credit import credit_service
from apps.billing.services.partner_invite import create_partner_channel
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(BILLING_ENABLED=True, PARTNER_CHANNEL_ENABLED=True)


def _owner() -> User:
    return User.objects.create_user(
        email=f"owner-{uuid.uuid4()}@example.com",
        password="secret123",
    )


@ENABLED
@pytest.mark.django_db
def test_clean_channel_reports_nothing() -> None:
    owner = _owner()
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    create_partner_channel(organization=org)

    call_command("partner_channel_reconcile")


@ENABLED
@pytest.mark.django_db
def test_missing_link_and_orphan_margin_are_reported(capsys) -> None:
    owner = _owner()
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    PartnerChannel.objects.create(
        organization=org,
        revenue_share_percent=Decimal("50.00"),
        is_active=True,
    )
    credit_service.credit(
        org.account,
        Decimal("1.000000"),
        reference_type=LedgerReferenceType.PARTNER_MARGIN,
        reference_id=str(uuid.uuid4()),
        idempotency_key=f"orphan-{uuid.uuid4()}",
    )

    with pytest.raises(SystemExit) as exc:
        call_command("partner_channel_reconcile")

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "has no invite link" in output
    assert "has no accrual" in output
    org.account.refresh_from_db()
    assert org.account.balance == Decimal("1.000000")
