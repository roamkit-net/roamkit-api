"""Tests for auth registration, JWT, and /me/."""

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

User = get_user_model()


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user() -> User:
    return User.objects.create_user(email="alice@example.com", password="SecurePass1!")


@pytest.mark.django_db
def test_register_creates_user(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps(
            {"email": "new@example.com", "password": "SecurePass1!"},
        ),
        content_type="application/json",
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["email"] == "new@example.com"
    assert "id" in payload
    assert User.objects.filter(email="new@example.com").exists()


@pytest.mark.django_db
def test_register_rejects_duplicate_email(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps(
            {"email": user.email, "password": "SecurePass1!"},
        ),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "email" in response.json()


@pytest.mark.django_db
def test_register_rejects_weak_password(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "weak@example.com", "password": "123"}),
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "password" in response.json()


@pytest.mark.django_db
def test_token_returns_jwt_for_valid_credentials(client: Client, user: User) -> None:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    assert "access" in payload
    assert "refresh" in payload


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
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
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
        data=json.dumps({"email": user.email, "password": "SecurePass1!"}),
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
