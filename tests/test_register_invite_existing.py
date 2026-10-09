"""Verified-invite registration for an account that already exists."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.core import mail
from django.db import connection
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.services.registration import (
    GENERIC_REGISTER_MESSAGE,
    RegistrationResult,
    register_user,
)
from apps.billing.partner_channel import PendingPartnerAttribution
from apps.billing.services.partner_invite import (
    canonical_invite_link,
    create_partner_channel,
    issue_join_signature,
)
from apps.billing.services.partner_pending import (
    sign_partner_pending,
    unsign_partner_pending,
)
from apps.organizations.services.account_binding import create_organization

ENABLED = override_settings(PARTNER_CHANNEL_ENABLED=True, BILLING_ENABLED=True)
PASSWORD = "SecurePass1!"
ACCOUNT_EXISTS = {
    "code": "account_exists",
    "detail": "An account with this email already exists.",
}


def _user(prefix: str, *, active: bool = True, email: str | None = None) -> User:
    user = User.objects.create_user(
        email=email or f"{prefix}-{uuid.uuid4()}@example.com",
        password=PASSWORD,
    )
    if not active:
        user.is_active = False
        user.set_unusable_password()
        user.save(update_fields=["password", "is_active", "updated_at"])
    return user


def _signed() -> str:
    owner = _user("owner")
    org = create_organization(name=f"Partner {uuid.uuid4()}", actor=owner)
    channel = create_partner_channel(organization=org)
    signed = issue_join_signature(canonical_invite_link(channel).token)
    assert signed is not None
    return signed


def _post(client: Client, email: str, pending: str | None):
    extra = {}
    if pending is not None:
        extra["HTTP_X_PARTNER_PENDING"] = pending
    return client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": email}),
        content_type="application/json",
        **extra,
    )


def _public_contract(response) -> tuple:
    return (
        response.status_code,
        response["Content-Type"],
        response.json(),
        response.content,
    )


def _expired_signature() -> str:
    issued = timezone.now()
    past = time.time() - (31 * 24 * 60 * 60)
    with patch("django.core.signing.time.time", return_value=past):
        signed = sign_partner_pending(visit_id=uuid.uuid4(), issued_at=issued)
    assert unsign_partner_pending(signed) is None
    return signed


def _unknown_visit_signature() -> str:
    signed = sign_partner_pending(visit_id=uuid.uuid4(), issued_at=timezone.now())
    assert unsign_partner_pending(signed) is not None
    return signed


@ENABLED
@pytest.mark.django_db
def test_valid_invite_active_account_returns_account_exists(client: Client) -> None:
    signed = _signed()
    existing = _user("existing")
    mail.outbox.clear()

    response = _post(client, existing.email, signed)

    assert response.status_code == 409
    assert response.json() == ACCOUNT_EXISTS
    assert mail.outbox == []
    assert not PendingPartnerAttribution.objects.filter(user=existing).exists()


@ENABLED
@pytest.mark.django_db
def test_valid_invite_inactive_account_resends_activation(client: Client) -> None:
    signed = _signed()
    pending = _user("pending", active=False)
    mail.outbox.clear()

    response = _post(client, pending.email, signed)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    assert "account_exists" not in response.json()
    assert len(mail.outbox) == 1
    assert "set-password" in mail.outbox[0].body
    assert pending.email in mail.outbox[0].to


@ENABLED
@pytest.mark.django_db
def test_valid_invite_new_account_sends_activation(client: Client) -> None:
    signed = _signed()
    email = f"new-{uuid.uuid4()}@example.com"
    mail.outbox.clear()

    response = _post(client, email, signed)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    user = User.objects.get(email=email)
    assert user.is_active is False
    assert PendingPartnerAttribution.objects.filter(user=user).exists()
    assert len(mail.outbox) == 1
    assert "set-password" in mail.outbox[0].body


@pytest.mark.django_db
def test_public_register_still_sends_reset(client: Client) -> None:
    existing = _user("public")
    mail.outbox.clear()

    response = _post(client, existing.email, None)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    assert len(mail.outbox) == 1
    assert "reset-password" in mail.outbox[0].body


@ENABLED
@pytest.mark.django_db
@pytest.mark.parametrize(
    "pending_for",
    ["invalid", "expired", "forged", "unknown_visit"],
)
def test_unverified_invite_hides_whether_the_account_exists(
    client: Client,
    pending_for: str,
) -> None:
    existing = _user("hidden")
    unknown = f"unknown-{uuid.uuid4()}@example.com"
    pending = {
        "invalid": "not-a-signature",
        "expired": _expired_signature(),
        "forged": "forged-pending",
        "unknown_visit": _unknown_visit_signature(),
    }[pending_for]
    mail.outbox.clear()

    existing_response = _post(client, existing.email, pending)
    unknown_response = _post(client, unknown, pending)

    assert _public_contract(existing_response) == _public_contract(unknown_response)
    assert existing_response.status_code == 200
    assert existing_response.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    assert "account_exists" not in existing_response.content.decode()
    resets = [item for item in mail.outbox if "/reset-password" in item.body]
    activations = [item for item in mail.outbox if "/set-password" in item.body]
    assert len(resets) == 1
    assert resets[0].to == [existing.email]
    assert len(activations) == 1
    assert unknown in activations[0].to


@ENABLED
@pytest.mark.django_db
def test_domain_case_uses_existing_normalizer(client: Client) -> None:
    signed = _signed()
    existing = _user("cased", email="user@example.com")
    assert existing.email == "user@example.com"
    mail.outbox.clear()

    folded = _post(client, "user@Example.com", signed)
    other_local = _post(client, "User@example.com", signed)

    assert folded.status_code == 409
    assert folded.json() == ACCOUNT_EXISTS
    assert other_local.status_code == 200
    assert other_local.json() == {"detail": GENERIC_REGISTER_MESSAGE}
    assert User.objects.filter(email="User@example.com").exists()
    assert not any("reset-password" in item.body for item in mail.outbox)
    assert len(mail.outbox) == 1
    assert "set-password" in mail.outbox[0].body


@ENABLED
@pytest.mark.django_db
def test_repeat_valid_invite_stays_account_exists_without_mail(client: Client) -> None:
    signed = _signed()
    existing = _user("repeat")
    mail.outbox.clear()

    first = _post(client, existing.email, signed)
    second = _post(client, existing.email, signed)

    assert first.json() == second.json() == ACCOUNT_EXISTS
    assert first.status_code == second.status_code == 409
    assert mail.outbox == []


@ENABLED
@pytest.mark.django_db(transaction=True)
def test_parallel_existing_account_submits_agree() -> None:
    signed = _signed()
    existing = _user("parallel")

    def once(_: int) -> str:
        connection.close()
        return register_user(
            email=existing.email,
            partner_pending=signed,
        ).value

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, range(2)))

    assert results == [
        RegistrationResult.ACCOUNT_EXISTS_FOR_INVITE.value,
        RegistrationResult.ACCOUNT_EXISTS_FOR_INVITE.value,
    ]
    assert mail.outbox == []
