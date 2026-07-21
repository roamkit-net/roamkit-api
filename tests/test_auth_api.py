"""Tests for auth registration, activation, password reset, JWT, and /me/."""

import json
from datetime import datetime, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.accounts.services.email import uid_for_user
from apps.accounts.tokens import account_activation_token, password_reset_token

User = get_user_model()

PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user() -> User:
    return User.objects.create_user(email="alice@example.com", password=PASSWORD)


@pytest.mark.django_db
def test_register_email_only_creates_pending_user(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "new@example.com"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert "detail" in response.json()
    user = User.objects.get(email="new@example.com")
    assert user.is_active is False
    assert not user.has_usable_password()
    assert len(mail.outbox) == 1
    assert "set-password" in mail.outbox[0].body


@pytest.mark.django_db
def test_register_duplicate_pending_resends_email(client: Client) -> None:
    first = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "pending@example.com"}),
        content_type="application/json",
    )
    assert first.status_code == 200
    assert len(mail.outbox) == 1

    second = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "pending@example.com"}),
        content_type="application/json",
    )
    assert second.status_code == 200
    assert len(mail.outbox) == 2
    assert User.objects.filter(email="pending@example.com").count() == 1


@pytest.mark.django_db
def test_register_existing_active_user_is_generic(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": user.email}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert "detail" in response.json()
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_register_rejects_invalid_email(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "not-an-email"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "email" in response.json()


@pytest.mark.django_db
def test_activate_sets_password_and_enables_login(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "activate@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="activate@example.com")
    uid = uid_for_user(user)
    token = account_activation_token.make_token(user)

    response = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid,
                "token": token,
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["email"] == "activate@example.com"

    user.refresh_from_db()
    assert user.is_active is True
    assert user.check_password(PASSWORD)

    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert token_response.status_code == 200
    assert "access" in token_response.json()


@pytest.mark.django_db
def test_activate_rejects_invalid_token(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "badtoken@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="badtoken@example.com")

    response = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": "invalid-token",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    user.refresh_from_db()
    assert user.is_active is False


@pytest.mark.django_db
def test_activate_rejects_password_mismatch(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "mismatch@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="mismatch@example.com")

    response = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": account_activation_token.make_token(user),
                "password": PASSWORD,
                "password_confirm": "DifferentPass1!",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "password_confirm" in response.json()


@pytest.mark.django_db
def test_activate_rejects_weak_password(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "weak@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="weak@example.com")

    response = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": account_activation_token.make_token(user),
                "password": "123",
                "password_confirm": "123",
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "password" in response.json()


@pytest.mark.django_db
def test_password_reset_request_always_200(client: Client, user: User) -> None:
    known = client.post(
        "/api/v1/auth/password-reset/",
        data=json.dumps({"email": user.email}),
        content_type="application/json",
    )
    unknown = client.post(
        "/api/v1/auth/password-reset/",
        data=json.dumps({"email": "nobody@example.com"}),
        content_type="application/json",
    )

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert len(mail.outbox) == 1
    assert "reset-password" in mail.outbox[0].body


@pytest.mark.django_db
def test_password_reset_confirm_updates_password(client: Client, user: User) -> None:
    uid = uid_for_user(user)
    token = password_reset_token.make_token(user)
    new_password = "NewSecurePass1!"

    response = client.post(
        "/api/v1/auth/password-reset/confirm/",
        data=json.dumps(
            {
                "uid": uid,
                "token": token,
                "password": new_password,
                "password_confirm": new_password,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.check_password(new_password)

    login = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": new_password}),
        content_type="application/json",
    )
    assert login.status_code == 200


@pytest.mark.django_db
def test_password_reset_confirm_rejects_invalid_token(
    client: Client,
    user: User,
) -> None:
    response = client.post(
        "/api/v1/auth/password-reset/confirm/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": "0-invalid",
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            }
        ),
        content_type="application/json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_activation_token_not_valid_for_password_reset(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "cross@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="cross@example.com")
    # Activate first so reset endpoint accepts an active user.
    activate = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": account_activation_token.make_token(user),
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            }
        ),
        content_type="application/json",
    )
    assert activate.status_code == 200
    user.refresh_from_db()

    activation_token = account_activation_token.make_token(user)
    response = client.post(
        "/api/v1/auth/password-reset/confirm/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": activation_token,
                "password": "AnotherPass1!",
                "password_confirm": "AnotherPass1!",
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_token_returns_jwt_for_valid_credentials(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": PASSWORD}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert "access" in payload
    assert "refresh" in payload


@pytest.mark.django_db
def test_token_rejects_inactive_user(client: Client) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "inactive@example.com"}),
        content_type="application/json",
    )
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": "inactive@example.com", "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 401


@pytest.mark.django_db
def test_token_rejects_invalid_credentials(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "wrong-password"}),
        content_type="application/json",
    )

    assert response.status_code == 401


@pytest.mark.django_db
def test_token_refresh(client: Client, user: User) -> None:
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": PASSWORD}),
        content_type="application/json",
    )
    refresh = token_response.json()["refresh"]

    response = client.post(
        "/api/v1/auth/token/refresh/",
        data=json.dumps({"refresh": refresh}),
        content_type="application/json",
    )

    assert response.status_code == 200
    assert "access" in response.json()


@pytest.mark.django_db
def test_me_requires_authentication(client: Client) -> None:
    response = client.get("/api/v1/auth/me/")

    assert response.status_code == 401


@pytest.mark.django_db
def test_me_returns_authenticated_user(client: Client, user: User) -> None:
    token_response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": PASSWORD}),
        content_type="application/json",
    )
    access = token_response.json()["access"]

    response = client.get(
        "/api/v1/auth/me/",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["email"] == user.email
    assert payload["id"] == user.pk


@pytest.mark.django_db
def test_packages_remain_public(client: Client) -> None:
    response = client.get("/api/v1/packages/")

    assert response.status_code == 200


@pytest.mark.django_db
def test_activation_token_expires(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "expired@example.com"}),
        content_type="application/json",
    )
    user = User.objects.get(email="expired@example.com")
    token = account_activation_token.make_token(user)

    original_now = account_activation_token._now

    def future_now() -> datetime:
        return original_now() + timedelta(hours=25)

    monkeypatch.setattr(account_activation_token, "_now", future_now)

    response = client.post(
        "/api/v1/auth/activate/",
        data=json.dumps(
            {
                "uid": uid_for_user(user),
                "token": token,
                "password": PASSWORD,
                "password_confirm": PASSWORD,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 400
