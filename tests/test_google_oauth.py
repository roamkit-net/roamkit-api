"""Google OAuth GIS ID-token auth (ADR 015)."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.providers.google.errors import GoogleAuthErrorCode
from apps.accounts.providers.google.verify import GoogleIdentity
from apps.billing.models import Account, CreditLedgerEntry, LedgerReferenceType
from apps.billing.partner_channel import (
    CustomerAttribution,
    InviteVisit,
    PendingPartnerAttribution,
)
from apps.billing.services.partner_invite import (
    canonical_invite_link,
    create_partner_channel,
    issue_join_signature,
)
from apps.billing.services.partner_invite_visit import ATTRIBUTION_WINDOW
from apps.billing.services.partner_pending import unsign_partner_pending
from apps.organizations.services.account_binding import create_organization

User = get_user_model()
PASSWORD = "test-pass-123"


def _identity(
    *,
    subject: str = "google-sub-1",
    email: str = "user@example.com",
    email_verified: bool = True,
    name: str = "Test User",
    picture: str = "https://example.com/p.png",
) -> GoogleIdentity:
    return GoogleIdentity(
        subject=subject,
        email=email,
        email_verified=email_verified,
        name=name,
        picture=picture,
    )


@pytest.fixture
def google_enabled(settings):
    settings.GOOGLE_OAUTH_ENABLED = True
    settings.GOOGLE_OAUTH_CLIENT_ID = "test-client-id.apps.googleusercontent.com"
    return settings


@pytest.mark.django_db
def test_google_404_when_disabled(client: Client, settings) -> None:
    settings.GOOGLE_OAUTH_ENABLED = False
    response = client.post(
        "/api/v1/auth/google/",
        data=json.dumps({"credential": "fake"}),
        content_type="application/json",
    )
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == GoogleAuthErrorCode.FEATURE_DISABLED


@pytest.mark.django_db
def test_google_creates_new_user(client: Client, google_enabled) -> None:
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"access", "refresh"}
    user = User.objects.get(email="user@example.com")
    assert user.google_sub == "google-sub-1"
    assert user.is_active is True
    assert not user.has_usable_password()
    assert user.last_login_provider == User.LastLoginProvider.GOOGLE
    assert user.google_name == "Test User"
    assert Account.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_google_auto_link_existing_password_user(
    client: Client, google_enabled
) -> None:
    user = User.objects.create_user(email="user@example.com", password=PASSWORD)
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.google_sub == "google-sub-1"
    assert user.has_usable_password()
    assert user.check_password(PASSWORD)


@pytest.mark.django_db
def test_google_activates_pending_register(client: Client, google_enabled) -> None:
    user = User(email="user@example.com", is_active=False)
    user.set_unusable_password()
    user.save()
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 200
    user.refresh_from_db()
    assert user.is_active is True
    assert user.google_sub == "google-sub-1"


@pytest.mark.django_db
def test_google_sub_conflict(client: Client, google_enabled) -> None:
    User.objects.create_user(
        email="other@example.com",
        password=PASSWORD,
        google_sub="google-sub-1",
    )
    User.objects.create_user(email="user@example.com", password=PASSWORD)
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(email="user@example.com", subject="google-sub-1"),
    ):
        # email user@ has no sub; but sub already on other@ — lookup by sub hits other
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    # Hits existing google_sub owner (other@) — success login as that user
    assert response.status_code == 200
    assert User.objects.filter(google_sub="google-sub-1").count() == 1

    # True conflict: email owned by user with different sub
    User.objects.filter(email="user@example.com").update(google_sub="other-sub")
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(email="user@example.com", subject="new-sub"),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 409
    assert response.json()["code"] == GoogleAuthErrorCode.SUB_CONFLICT


@pytest.mark.django_db
def test_google_email_not_verified(client: Client, google_enabled) -> None:
    from apps.accounts.providers.google.errors import GoogleAuthError

    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        side_effect=GoogleAuthError(GoogleAuthErrorCode.EMAIL_NOT_VERIFIED),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 400
    assert response.json()["code"] == GoogleAuthErrorCode.EMAIL_NOT_VERIFIED


@pytest.mark.django_db
def test_google_inactive_password_account(client: Client, google_enabled) -> None:
    user = User.objects.create_user(email="user@example.com", password=PASSWORD)
    user.is_active = False
    user.save(update_fields=["is_active"])
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 401
    assert response.json()["code"] == GoogleAuthErrorCode.ACCOUNT_DISABLED


@pytest.mark.django_db
def test_google_bad_token(client: Client, google_enabled) -> None:
    from apps.accounts.providers.google.errors import GoogleAuthError

    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        side_effect=GoogleAuthError(GoogleAuthErrorCode.INVALID_TOKEN),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "bad"}),
            content_type="application/json",
        )
    assert response.status_code == 400
    assert response.json()["code"] == GoogleAuthErrorCode.INVALID_TOKEN


@pytest.mark.django_db
def test_google_normalizes_email(client: Client, google_enabled) -> None:
    User.objects.create_user(email="user@example.com", password=PASSWORD)
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(email="  USER@Example.COM "),
    ):
        response = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert response.status_code == 200
    assert User.objects.filter(google_sub="google-sub-1").count() == 1
    assert User.objects.get(google_sub="google-sub-1").email == "user@example.com"


@pytest.mark.django_db(transaction=True)
def test_google_race_same_email_one_sub(google_enabled) -> None:
    def _login() -> int:
        c = Client()
        with patch(
            "apps.accounts.providers.google.service.verify_google_id_token",
            return_value=_identity(subject="race-sub", email="race@example.com"),
        ):
            r = c.post(
                "/api/v1/auth/google/",
                data=json.dumps({"credential": "tok"}),
                content_type="application/json",
            )
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: _login(), range(2)))
    assert all(s == 200 for s in statuses)
    assert User.objects.filter(email="race@example.com").count() == 1
    assert User.objects.filter(google_sub="race-sub").count() == 1


@pytest.mark.django_db
def test_google_idempotent_triple_post(client: Client, google_enabled) -> None:
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        for _ in range(3):
            response = client.post(
                "/api/v1/auth/google/",
                data=json.dumps({"credential": "tok"}),
                content_type="application/json",
            )
            assert response.status_code == 200
            assert "access" in response.json()
    assert User.objects.filter(email="user@example.com").count() == 1
    assert User.objects.filter(google_sub="google-sub-1").count() == 1


@pytest.mark.django_db
def test_google_password_token_schema_parity(client: Client, google_enabled) -> None:
    User.objects.create_user(email="user@example.com", password=PASSWORD)
    password_resp = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": "user@example.com", "password": PASSWORD}),
        content_type="application/json",
    )
    assert password_resp.status_code == 200
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(),
    ):
        google_resp = client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
        )
    assert google_resp.status_code == 200
    assert set(password_resp.json().keys()) == set(google_resp.json().keys())
    assert set(google_resp.json().keys()) == {"access", "refresh"}
    user = User.objects.get(email="user@example.com")
    assert user.last_login_provider == User.LastLoginProvider.GOOGLE


@pytest.mark.django_db
def test_password_sets_last_login_provider(client: Client) -> None:
    User.objects.create_user(email="user@example.com", password=PASSWORD)
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": "user@example.com", "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 200
    user = User.objects.get(email="user@example.com")
    assert user.last_login_provider == User.LastLoginProvider.PASSWORD


def test_google_provider_import_boundary() -> None:
    """Billing/orders/esims must not import Google provider code."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "apps"
    offenders: list[str] = []
    needle = "accounts.providers.google"
    for app in ("billing", "orders", "esims"):
        for path in (root / app).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if needle in text or "google.oauth2" in text or "google.auth" in text:
                offenders.append(str(path.relative_to(root.parent.parent)))
    assert offenders == [], f"Google imports outside accounts: {offenders}"


PARTNER = override_settings(
    PARTNER_CHANNEL_ENABLED=True,
    PARTNER_JOIN_BASE_URL="https://roamkit.net",
)


def _signed_visit(bonus: str) -> str:
    owner = User.objects.create_user(
        email=f"owner-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    channel = create_partner_channel(organization=org)
    link = canonical_invite_link(channel)
    link.bonus_amount = Decimal(bonus)
    link.save(update_fields=["bonus_amount", "updated_at"])
    signed = issue_join_signature(link.token)
    assert signed is not None
    return signed


def _google(client: Client, email: str, *, pending: str | None, subject: str):
    headers = {}
    if pending is not None:
        headers["HTTP_X_PARTNER_PENDING"] = pending
    with patch(
        "apps.accounts.providers.google.service.verify_google_id_token",
        return_value=_identity(email=email, subject=subject),
    ):
        return client.post(
            "/api/v1/auth/google/",
            data=json.dumps({"credential": "tok"}),
            content_type="application/json",
            **headers,
        )


@PARTNER
@pytest.mark.django_db
def test_google_created_visit_snapshots_current_bonus(
    client: Client, google_enabled
) -> None:
    email = f"new-{uuid.uuid4()}@example.com"
    signed = _signed_visit("10.000000")
    response = _google(client, email, pending=signed, subject=f"sub-{uuid.uuid4()}")
    assert response.status_code == 200
    assert set(response.json()) == {"access", "refresh"}
    user = User.objects.get(email=email)
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.registered_via_invite is True
    assert attribution.bonus_amount_snapshot == Decimal("10.000000")
    assert attribution.invite_visit_id == unsign_partner_pending(signed)["visit_id"]
    entry = CreditLedgerEntry.objects.get(account=user.billing_account)
    assert entry.delta == Decimal("10.000000")
    assert entry.reference_type == LedgerReferenceType.PARTNER_INVITE_BONUS
    assert entry.reference_id == str(attribution.id)
    assert entry.idempotency_key == f"invite-registration-bonus:{user.pk}"
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("10.000000")


@PARTNER
@pytest.mark.django_db
def test_google_created_zero_bonus_and_invalid_visit(
    client: Client, google_enabled
) -> None:
    zero_email = f"zero-{uuid.uuid4()}@example.com"
    signed = _signed_visit("0.000000")
    assert (
        _google(
            client, zero_email, pending=signed, subject=f"sub-{uuid.uuid4()}"
        ).status_code
        == 200
    )
    zero = CustomerAttribution.objects.get(user__email=zero_email)
    assert zero.registered_via_invite is True
    assert zero.bonus_amount_snapshot == Decimal("0.000000")
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS,
        account__user__email=zero_email,
    ).exists()

    bad_email = f"bad-{uuid.uuid4()}@example.com"
    assert (
        _google(
            client, bad_email, pending="not-signed", subject=f"sub-{uuid.uuid4()}"
        ).status_code
        == 200
    )
    assert User.objects.filter(email=bad_email).exists()
    assert not CustomerAttribution.objects.filter(user__email=bad_email).exists()

    old_email = f"old-{uuid.uuid4()}@example.com"
    old_signed = _signed_visit("4.000000")
    InviteVisit.objects.filter(
        pk=unsign_partner_pending(old_signed)["visit_id"]
    ).update(created_at=timezone.now() - ATTRIBUTION_WINDOW - timedelta(seconds=1))
    assert (
        _google(
            client, old_email, pending=old_signed, subject=f"sub-{uuid.uuid4()}"
        ).status_code
        == 200
    )
    assert not CustomerAttribution.objects.filter(user__email=old_email).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@PARTNER
@pytest.mark.django_db
def test_google_existing_consume_does_not_mark_registration(
    client: Client, google_enabled
) -> None:
    email = f"has-{uuid.uuid4()}@example.com"
    subject = f"sub-{uuid.uuid4()}"
    user = User.objects.create_user(
        email=email,
        password=PASSWORD,
        google_sub=subject,
    )
    signed = _signed_visit("8.000000")
    assert _google(client, email, pending=signed, subject=subject).status_code == 200
    attribution = CustomerAttribution.objects.get(user=user)
    assert attribution.registered_via_invite is False
    assert attribution.bonus_amount_snapshot is None

    other = _signed_visit("9.000000")
    assert _google(client, email, pending=other, subject=subject).status_code == 200
    attribution.refresh_from_db()
    assert attribution.registered_via_invite is False
    assert attribution.bonus_amount_snapshot is None
    assert attribution.invite_visit_id == unsign_partner_pending(signed)["visit_id"]
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@PARTNER
@pytest.mark.django_db
def test_google_existing_pending_is_left_for_email_confirmation(
    client: Client, google_enabled
) -> None:
    email = f"pend-{uuid.uuid4()}@example.com"
    subject = f"sub-{uuid.uuid4()}"
    user = User.objects.create_user(email=email, password=PASSWORD, google_sub=subject)
    signed = _signed_visit("6.000000")
    visit_id = unsign_partner_pending(signed)["visit_id"]
    visit = InviteVisit.objects.select_related("invite_link").get(pk=visit_id)
    PendingPartnerAttribution.objects.create(
        user=user,
        partner_channel_id=visit.invite_link.partner_channel_id,
        invite_token_snapshot=visit.invite_link.token,
        invite_visit=visit,
        expires_at=timezone.now() + timedelta(hours=24),
    )
    response = _google(client, email, pending=signed, subject=subject)
    assert response.status_code == 200
    assert not CustomerAttribution.objects.filter(user=user).exists()
    assert PendingPartnerAttribution.objects.filter(
        user=user, invite_visit=visit
    ).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()


@PARTNER
@pytest.mark.django_db
def test_google_created_credits_twenty_and_linked_user_does_not(
    client: Client, google_enabled
) -> None:
    created_email = f"twenty-{uuid.uuid4()}@example.com"
    signed = _signed_visit("20.000000")
    assert (
        _google(
            client, created_email, pending=signed, subject=f"sub-{uuid.uuid4()}"
        ).status_code
        == 200
    )
    created = User.objects.get(email=created_email)
    assert CreditLedgerEntry.objects.get(account=created.billing_account).delta == (
        Decimal("20.000000")
    )

    linked_email = f"linked-{uuid.uuid4()}@example.com"
    User.objects.create_user(email=linked_email, password=PASSWORD)
    linked_signed = _signed_visit("12.000000")
    assert (
        _google(
            client,
            linked_email,
            pending=linked_signed,
            subject=f"sub-{uuid.uuid4()}",
        ).status_code
        == 200
    )
    linked = CustomerAttribution.objects.get(user__email=linked_email)
    assert linked.registered_via_invite is False
    assert linked.bonus_amount_snapshot is None
    assert (
        CreditLedgerEntry.objects.filter(
            reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
        ).count()
        == 1
    )


@PARTNER
@pytest.mark.django_db
def test_google_credit_failure_rolls_back_user_and_retry_credits_once(
    client: Client, google_enabled
) -> None:
    email = f"rollback-{uuid.uuid4()}@example.com"
    subject = f"sub-{uuid.uuid4()}"
    signed = _signed_visit("10.000000")
    with (
        patch(
            "apps.billing.services.partner_attribution.credit_service.credit",
            side_effect=RuntimeError("ledger down"),
        ),
        pytest.raises(RuntimeError, match="ledger down"),
    ):
        _google(client, email, pending=signed, subject=subject)
    assert not User.objects.filter(email=email).exists()
    assert not CreditLedgerEntry.objects.filter(
        reference_type=LedgerReferenceType.PARTNER_INVITE_BONUS
    ).exists()

    assert _google(client, email, pending=signed, subject=subject).status_code == 200
    user = User.objects.get(email=email)
    entries = CreditLedgerEntry.objects.filter(
        idempotency_key=f"invite-registration-bonus:{user.pk}"
    )
    assert entries.count() == 1
    user.billing_account.refresh_from_db()
    assert user.billing_account.balance == Decimal("10.000000")
