"""Invite link, join signature, pending attribution, and consume (ADR 023)."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.models import User
from apps.accounts.services.email import uid_for_user
from apps.accounts.services.registration import (
    GENERIC_REGISTER_MESSAGE,
    RegistrationResult,
    activate_user,
    register_user,
)
from apps.accounts.tokens import account_activation_token
from apps.billing.models import CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    InviteVisit,
    PartnerInviteLink,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_attribution import (
    consume_partner_pending,
    credit_registration_invite_bonus,
)
from apps.billing.services.partner_invite import (
    canonical_invite_link,
    create_partner_channel,
    issue_join_signature,
    regenerate_invite_link,
)
from apps.billing.services.partner_invite_visit import ATTRIBUTION_WINDOW
from apps.billing.services.partner_pending import unsign_partner_pending
from apps.organizations.models import Membership, MembershipRole, MembershipStatus
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    BILLING_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)
PASSWORD = "SecurePass1!"


def _user(prefix: str, *, active: bool = True) -> User:
    user = User.objects.create_user(
        email=f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    if not active:
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["password", "is_active", "updated_at"])
    return user


def _channel(actor: User, *, active: bool = True):
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=actor)
    channel = create_partner_channel(organization=org)
    if not active:
        channel.is_active = False
        channel.save(update_fields=["is_active", "updated_at"])
    return channel


def _activate(user: User) -> None:
    activate_user(
        uid=uid_for_user(user),
        token=account_activation_token.make_token(user),
        password=PASSWORD,
        password_confirm=PASSWORD,
    )


def _auth(user: User) -> dict[str, str]:
    access = str(RefreshToken.for_user(user).access_token)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}


def _role(channel, user: User, role: str) -> None:
    Membership.objects.create(
        organization=channel.organization,
        user=user,
        role=role,
        status=MembershipStatus.ACTIVE,
    )


@ENABLED
@pytest.mark.django_db
def test_owner_reads_and_regenerates_one_link(caplog, client: Client) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    old = canonical_invite_link(channel).token
    pending_user = _user("pending", active=False)
    PendingPartnerAttribution.objects.create(
        user=pending_user,
        partner_channel=channel,
        invite_token_snapshot=old,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    read = client.get("/api/v1/orgs/partner/invite-link/", **_auth(owner))
    assert read.status_code == 200
    assert read["Cache-Control"] == "no-store"
    assert set(read.json()) == {"url", "is_active", "created_at", "regenerated_at"}
    assert read.json()["url"] == f"https://roamkit.net/join/{old}"
    assert read.json()["regenerated_at"] is None
    assert read["X-Partner-Role"] == "owner"

    with caplog.at_level(logging.INFO):
        regenerated = client.post(
            "/api/v1/orgs/partner/invite-link/regenerate/",
            **_auth(owner),
        )
    assert regenerated.status_code == 200
    assert "X-Partner-Role" not in regenerated
    assert regenerated.json()["url"] != read.json()["url"]
    assert issue_join_signature(old) is None
    assert not PendingPartnerAttribution.objects.filter(user=pending_user).exists()
    assert PartnerInviteLink.objects.filter(partner_channel=channel).count() == 1
    assert old not in caplog.text
    assert "partner_invite.regenerated" in caplog.text


@ENABLED
@pytest.mark.django_db
def test_viewer_reads_and_cannot_mutate(client: Client) -> None:
    owner = _user("owner")
    viewer = _user("viewer")
    admin = _user("admin")
    channel = _channel(owner)
    _role(channel, viewer, MembershipRole.VIEWER)
    _role(channel, admin, MembershipRole.ADMIN)

    read = client.get(
        "/api/v1/orgs/partner/invite-link/",
        HTTP_X_PARTNER_ROLE="owner",
        **_auth(viewer),
    )
    assert read.status_code == 200
    assert read["X-Partner-Role"] == "viewer"
    denied = client.post(
        "/api/v1/orgs/partner/invite-link/deactivate/",
        HTTP_X_PARTNER_ROLE="owner",
        **_auth(viewer),
    )
    admin_denied = client.post(
        "/api/v1/orgs/partner/invite-link/activate/",
        **_auth(admin),
    )
    assert denied.status_code == 403
    assert denied.json() == {"code": "partner_invite_forbidden"}
    assert "X-Partner-Role" not in denied
    assert admin_denied.json() == {"code": "partner_invite_forbidden"}
    assert canonical_invite_link(channel).is_active is True


@ENABLED
@pytest.mark.django_db
def test_repeat_deactivate_does_not_audit(caplog, client: Client) -> None:
    owner = _user("owner")
    _channel(owner)
    client.post("/api/v1/orgs/partner/invite-link/deactivate/", **_auth(owner))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        again = client.post(
            "/api/v1/orgs/partner/invite-link/deactivate/",
            **_auth(owner),
        )
    assert again.status_code == 200
    assert "X-Partner-Role" not in again
    assert again.json()["is_active"] is False
    assert "partner_invite.deactivated" not in caplog.text


@pytest.mark.django_db
def test_flag_off_hides_invite_and_join(client: Client) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    token = canonical_invite_link(channel).token

    response = client.get("/api/v1/orgs/partner/invite-link/", **_auth(owner))
    signed = client.post(
        "/api/internal/partner/join-sign/",
        data={"token": token},
        content_type="application/json",
    )

    assert response.status_code == 404
    assert response.json() == {"code": "partner_channel_disabled"}
    assert signed.status_code == 404
    assert signed.content == b""


@ENABLED
@pytest.mark.django_db
def test_inactive_channel_still_signs_and_inactive_link_does_not() -> None:
    owner = _user("owner")
    channel = _channel(owner, active=False)
    link = canonical_invite_link(channel)
    token = link.token
    assert issue_join_signature(token) is not None
    link.is_active = False
    link.save(update_fields=["is_active"])
    assert issue_join_signature(token) is None
    assert issue_join_signature("missing") is None


@ENABLED
@pytest.mark.django_db
def test_register_activation_creates_attribution() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)
    email = f"new-{uuid.uuid4()}@example.com"
    result = register_user(email=email, partner_pending=signed)
    user = User.objects.get(email=email)
    pending = PendingPartnerAttribution.objects.get(user=user)
    assert result is RegistrationResult.CREATED
    assert pending.invite_visit_id is not None

    _activate(user)
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.partner_channel_id == channel.pk
    assert attribution.source == CustomerAttribution.Source.INVITE_LINK
    assert attribution.registered_via_invite is True
    assert attribution.invite_visit_id == pending.invite_visit_id
    assert not PendingPartnerAttribution.objects.filter(user=user).exists()


@ENABLED
@pytest.mark.django_db
def test_existing_user_consume_and_second_partner_is_noop() -> None:
    owner = _user("owner")
    other_owner = _user("other")
    customer = _user("customer")
    channel = _channel(owner)
    other = _channel(other_owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)

    assert consume_partner_pending(customer, signed) == "created"
    attribution = CustomerAttribution.objects.get(user=customer)
    assert attribution.partner_channel_id == channel.pk
    assert attribution.registered_via_invite is False
    assert attribution.bonus_amount_snapshot is None
    assert attribution.invite_visit_id is not None

    other_signed = issue_join_signature(canonical_invite_link(other).token)
    assert consume_partner_pending(customer, other_signed) == "noop"
    assert (
        CustomerAttribution.objects.get(user=customer).partner_channel_id == channel.pk
    )


@ENABLED
@pytest.mark.django_db
def test_old_token_consume_is_ignored() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)
    regenerate_invite_link(channel, actor=owner, request_id="req")
    assert unsign_partner_pending(signed) is not None
    assert consume_partner_pending(customer, signed) == "ignored"
    assert not CustomerAttribution.objects.filter(user=customer).exists()


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_parallel_consumes_create_one_attribution() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)

    def once(_: int) -> str:
        connection.close()
        return consume_partner_pending(customer, signed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, range(2)))

    assert sorted(results) == ["created", "noop"]
    assert CustomerAttribution.objects.filter(user=customer).count() == 1


@ENABLED
@pytest.mark.django_db
def test_cleanup_deletes_only_expired_pending() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    expired_user = _user("expired", active=False)
    fresh_user = _user("fresh", active=False)
    attributed = _user("kept")
    CustomerAttribution.objects.create(
        user=attributed,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    PendingPartnerAttribution.objects.create(
        user=expired_user,
        partner_channel=channel,
        invite_token_snapshot=canonical_invite_link(channel).token,
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    PendingPartnerAttribution.objects.create(
        user=fresh_user,
        partner_channel=channel,
        invite_token_snapshot=canonical_invite_link(channel).token,
        expires_at=timezone.now() + timedelta(hours=1),
    )

    call_command("cleanup_expired_partner_attributions")

    assert not PendingPartnerAttribution.objects.filter(user=expired_user).exists()
    assert PendingPartnerAttribution.objects.filter(user=fresh_user).exists()
    assert CustomerAttribution.objects.filter(user=attributed).exists()


@ENABLED
@pytest.mark.django_db
def test_register_invalid_or_old_visit_creates_user_without_pending() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)
    visit_id = unsign_partner_pending(signed)["visit_id"]
    InviteVisit.objects.filter(pk=visit_id).update(
        created_at=timezone.now() - ATTRIBUTION_WINDOW - timedelta(seconds=1)
    )
    old_email = f"old-{uuid.uuid4()}@example.com"
    assert (
        register_user(email=old_email, partner_pending=signed)
        is RegistrationResult.CREATED
    )
    old_user = User.objects.get(email=old_email)
    assert not PendingPartnerAttribution.objects.filter(user=old_user).exists()

    bad_email = f"bad-{uuid.uuid4()}@example.com"
    assert (
        register_user(email=bad_email, partner_pending="not-a-signature")
        is RegistrationResult.CREATED
    )
    assert not PendingPartnerAttribution.objects.filter(user__email=bad_email).exists()


@ENABLED
@pytest.mark.django_db
def test_existing_register_does_not_create_pending(client: Client) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    signed = issue_join_signature(canonical_invite_link(channel).token)
    existing = _user("existing")
    assert (
        register_user(email=existing.email, partner_pending=signed)
        is RegistrationResult.ACCOUNT_EXISTS_FOR_INVITE
    )
    assert not PendingPartnerAttribution.objects.filter(user=existing).exists()

    email = f"public-{uuid.uuid4()}@example.com"
    first = client.post(
        "/api/v1/auth/register/",
        data={"email": email},
        content_type="application/json",
        HTTP_X_PARTNER_PENDING=signed,
    )
    second = client.post(
        "/api/v1/auth/register/",
        data={"email": email},
        content_type="application/json",
        HTTP_X_PARTNER_PENDING=signed,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    assert PendingPartnerAttribution.objects.filter(user__email=email).count() == 1


@ENABLED
@pytest.mark.django_db
def test_confirmation_snapshots_current_bonus_and_survives_day_30() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    link.bonus_amount = Decimal("5.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    signed = issue_join_signature(
        link.token,
        {"utm_source": "tiktok", "utm_medium": "social"},
    )
    link.bonus_amount = Decimal("10.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    visit_id = unsign_partner_pending(signed)["visit_id"]
    InviteVisit.objects.filter(pk=visit_id).update(
        created_at=timezone.now() - timedelta(days=29)
    )
    email = f"bonus-{uuid.uuid4()}@example.com"
    register_user(email=email, partner_pending=signed)
    InviteVisit.objects.filter(pk=visit_id).update(
        created_at=timezone.now() - ATTRIBUTION_WINDOW - timedelta(days=1)
    )
    user = User.objects.get(email=email)
    _activate(user)
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.registered_via_invite is True
    assert attribution.bonus_amount_snapshot == Decimal("10.000000")
    assert attribution.utm_source_snapshot == "tiktok"
    assert attribution.utm_medium_snapshot == "social"
    assert attribution.utm_campaign_snapshot == ""
    assert attribution.invite_visit_id == visit_id


@ENABLED
@pytest.mark.django_db
def test_confirmation_drops_expired_inactive_or_regenerated_pending() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    link = canonical_invite_link(channel)

    expired_email = f"expired-{uuid.uuid4()}@example.com"
    register_user(
        email=expired_email,
        partner_pending=issue_join_signature(link.token),
    )
    expired = User.objects.get(email=expired_email)
    pending = PendingPartnerAttribution.objects.get(user=expired)
    pending.expires_at = timezone.now() - timedelta(minutes=1)
    pending.save(update_fields=["expires_at"])
    _activate(expired)
    assert not CustomerAttribution.objects.filter(user=expired).exists()
    assert not PendingPartnerAttribution.objects.filter(user=expired).exists()

    inactive_email = f"inactive-{uuid.uuid4()}@example.com"
    register_user(
        email=inactive_email,
        partner_pending=issue_join_signature(link.token),
    )
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    inactive = User.objects.get(email=inactive_email)
    _activate(inactive)
    assert not CustomerAttribution.objects.filter(user=inactive).exists()
    assert not PendingPartnerAttribution.objects.filter(user=inactive).exists()
    assert InviteVisit.objects.filter(invite_link=link).count() == 2

    link.is_active = True
    link.save(update_fields=["is_active", "updated_at"])
    regenerated_email = f"regen-{uuid.uuid4()}@example.com"
    signed = issue_join_signature(link.token)
    register_user(email=regenerated_email, partner_pending=signed)
    visit_id = unsign_partner_pending(signed)["visit_id"]
    link.regenerated_at = timezone.now()
    link.save(update_fields=["regenerated_at", "updated_at"])
    regenerated = User.objects.get(email=regenerated_email)
    _activate(regenerated)
    assert not CustomerAttribution.objects.filter(user=regenerated).exists()
    assert not PendingPartnerAttribution.objects.filter(user=regenerated).exists()
    assert InviteVisit.objects.filter(pk=visit_id).exists()


@ENABLED
@pytest.mark.django_db
def test_campaign_link_confirmation_does_not_use_canonical_link() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    canonical = canonical_invite_link(channel)
    campaign = PartnerInviteLink.objects.create(
        partner_channel=channel,
        token=f"campaign-{uuid.uuid4().hex}",
        name="Fall",
        bonus_amount=Decimal("3.000000"),
        source="ads",
        campaign="fall",
        content="video",
    )
    signed = issue_join_signature(
        campaign.token,
        {"utm_source": "news", "utm_content": "cta"},
    )
    email = f"campaign-{uuid.uuid4()}@example.com"
    register_user(email=email, partner_pending=signed)
    user = User.objects.get(email=email)
    _activate(user)
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.invite_link_id == campaign.pk
    assert attribution.invite_link_id != canonical.pk
    assert attribution.partner_channel_id == channel.pk
    assert attribution.registered_via_invite is True
    assert attribution.bonus_amount_snapshot == Decimal("3.000000")
    assert attribution.invite_name_snapshot == "Fall"
    assert attribution.invite_source_snapshot == "ads"
    assert attribution.invite_campaign_snapshot == "fall"
    assert attribution.invite_content_snapshot == "video"
    assert attribution.utm_source_snapshot == "news"
    assert attribution.utm_content_snapshot == "cta"


@ENABLED
@pytest.mark.django_db
def test_consume_rejects_old_inactive_visit_and_keeps_email_pending() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    signed = issue_join_signature(link.token, {"utm_source": "x"})
    visit_id = unsign_partner_pending(signed)["visit_id"]
    InviteVisit.objects.filter(pk=visit_id).update(
        created_at=timezone.now() - ATTRIBUTION_WINDOW - timedelta(seconds=1)
    )
    assert consume_partner_pending(customer, signed) == "ignored"
    assert not CustomerAttribution.objects.filter(user=customer).exists()

    fresh = issue_join_signature(link.token)
    link.is_active = False
    link.save(update_fields=["is_active", "updated_at"])
    assert consume_partner_pending(customer, fresh) == "ignored"

    link.is_active = True
    link.save(update_fields=["is_active", "updated_at"])
    pending_user = _user("pending-user")
    pending_signed = issue_join_signature(link.token)
    PendingPartnerAttribution.objects.create(
        user=pending_user,
        partner_channel=channel,
        invite_token_snapshot=link.token,
        invite_visit_id=unsign_partner_pending(pending_signed)["visit_id"],
        expires_at=timezone.now() + timedelta(hours=24),
    )
    assert consume_partner_pending(pending_user, pending_signed) == "ignored"
    assert not CustomerAttribution.objects.filter(user=pending_user).exists()
    assert PendingPartnerAttribution.objects.filter(user=pending_user).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "amount",
    ["5.000000", "10.000000", "20.000000", "0.500000"],
)
def test_email_bonus_credits_the_user_account_only(amount: str) -> None:
    owner = _user("owner")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    link.bonus_amount = Decimal(amount)
    link.save(update_fields=["bonus_amount", "updated_at"])
    team = channel.organization.account
    team_before = team.balance
    email = f"bonus-{uuid.uuid4()}@example.com"
    register_user(email=email, partner_pending=issue_join_signature(link.token))
    user = User.objects.get(email=email)
    _activate(user)
    attribution = CustomerAttribution.objects.get(user=user)
    entry = CreditLedgerEntry.objects.get(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    )
    assert entry.delta == Decimal(amount)
    assert entry.reference_id == str(attribution.id)
    assert entry.idempotency_key == f"invite-registration-bonus:{user.pk}"
    user.billing_account.refresh_from_db()
    team.refresh_from_db()
    assert user.billing_account.balance == Decimal(amount)
    assert team.balance == team_before


@ENABLED
@pytest.mark.django_db
def test_zero_bonus_and_bonus_change_before_confirmation() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    zero_email = f"zero-{uuid.uuid4()}@example.com"
    register_user(email=zero_email, partner_pending=issue_join_signature(link.token))
    _activate(User.objects.get(email=zero_email))
    assert CustomerAttribution.objects.filter(user__email=zero_email).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()

    link.bonus_amount = Decimal("10.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    signed = issue_join_signature(link.token)
    link.bonus_amount = Decimal("20.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    changed_email = f"changed-{uuid.uuid4()}@example.com"
    register_user(email=changed_email, partner_pending=signed)
    user = User.objects.get(email=changed_email)
    _activate(user)
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.bonus_amount_snapshot == Decimal("20.000000")
    entry = CreditLedgerEntry.objects.get(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    )
    assert entry.delta == Decimal("20.000000")
    assert entry.reference_id == str(attribution.id)


@ENABLED
@pytest.mark.django_db
def test_existing_consume_and_admin_do_not_credit() -> None:
    owner = _user("owner")
    customer = _user("customer")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    link.bonus_amount = Decimal("15.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    signed = issue_join_signature(link.token)
    assert consume_partner_pending(customer, signed) == "created"
    consumed = CustomerAttribution.objects.get(user=customer)
    assert consumed.registered_via_invite is False
    assert consumed.bonus_amount_snapshot is None
    credit_registration_invite_bonus(attribution=consumed)

    admin_user = _user("admin-customer")
    admin_row = CustomerAttribution.objects.create(
        user=admin_user,
        partner_channel=channel,
        source=CustomerAttribution.Source.ADMIN,
        attributed_at=timezone.now(),
    )
    credit_registration_invite_bonus(attribution=admin_row)
    marked = CustomerAttribution(
        user=_user("marked"),
        partner_channel=channel,
        source=CustomerAttribution.Source.INVITE_LINK,
        registered_via_invite=False,
        bonus_amount_snapshot=Decimal("10.000000"),
        attributed_at=timezone.now(),
    )
    marked.save()
    credit_registration_invite_bonus(attribution=marked)
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@ENABLED
@pytest.mark.django_db
def test_credit_failure_rolls_back_activation_and_retry_credits_once() -> None:
    owner = _user("owner")
    channel = _channel(owner)
    link = canonical_invite_link(channel)
    link.bonus_amount = Decimal("10.000000")
    link.save(update_fields=["bonus_amount", "updated_at"])
    email = f"retry-{uuid.uuid4()}@example.com"
    register_user(email=email, partner_pending=issue_join_signature(link.token))
    user = User.objects.get(email=email)
    uid = uid_for_user(user)
    token = account_activation_token.make_token(user)
    with (
        patch(
            "apps.billing.services.partner_attribution.credit_service.credit",
            side_effect=RuntimeError("ledger down"),
        ),
        pytest.raises(RuntimeError, match="ledger down"),
    ):
        activate_user(
            uid=uid,
            token=token,
            password=PASSWORD,
            password_confirm=PASSWORD,
        )
    user.refresh_from_db()
    assert user.is_active is False
    assert not user.has_usable_password()
    assert PendingPartnerAttribution.objects.filter(user=user).exists()
    assert not CustomerAttribution.objects.filter(user=user).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()

    _activate(user)
    user.refresh_from_db()
    attribution = CustomerAttribution.objects.get(user=user)
    credit_registration_invite_bonus(attribution=attribution)
    entries = CreditLedgerEntry.objects.filter(
        idempotency_key=f"invite-registration-bonus:{user.pk}"
    )
    assert entries.count() == 1
    assert entries.get().delta == Decimal("10.000000")
    assert entries.get().reference_id == str(attribution.id)
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("10.000000")
    assert not PendingPartnerAttribution.objects.filter(user=user).exists()
