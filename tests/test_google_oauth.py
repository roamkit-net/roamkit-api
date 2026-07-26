"""Google OAuth GIS ID-token auth (ADR 015)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.providers.google.errors import GoogleAuthErrorCode
from apps.accounts.providers.google.verify import GoogleIdentity
from apps.billing.models import Account

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
