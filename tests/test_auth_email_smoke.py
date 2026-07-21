"""Local smoke: register → activation email → activate → login → reset."""

from __future__ import annotations

import json
import re

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

User = get_user_model()

PASSWORD = "SecurePass1!"
NEW_PASSWORD = "NewSecurePass1!"


def _extract_query(body: str, key: str) -> str:
    match = re.search(rf"[?&]{key}=([^&\s]+)", body)
    assert match, f"missing {key} in email body: {body}"
    return match.group(1)


@pytest.mark.django_db
def test_auth_email_smoke_register_activate_login_reset(client: Client) -> None:
    email = "smoke-auth@example.com"

    register = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": email}),
        content_type="application/json",
    )
    assert register.status_code == 200
    assert len(mail.outbox) == 1
    activation_body = mail.outbox[0].body
    assert "/set-password?" in activation_body
    uid = _extract_query(activation_body, "uid")
    token = _extract_query(activation_body, "token")

    activate = client.post(
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
    assert activate.status_code == 200
    assert activate.json()["email"] == email

    login = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert login.status_code == 200
    assert login.json()["access"]

    reset_req = client.post(
        "/api/v1/auth/password-reset/",
        data=json.dumps({"email": email}),
        content_type="application/json",
    )
    assert reset_req.status_code == 200
    assert len(mail.outbox) == 2
    reset_body = mail.outbox[1].body
    assert "/reset-password?" in reset_body
    reset_uid = _extract_query(reset_body, "uid")
    reset_token = _extract_query(reset_body, "token")

    reset_confirm = client.post(
        "/api/v1/auth/password-reset/confirm/",
        data=json.dumps(
            {
                "uid": reset_uid,
                "token": reset_token,
                "password": NEW_PASSWORD,
                "password_confirm": NEW_PASSWORD,
            }
        ),
        content_type="application/json",
    )
    assert reset_confirm.status_code == 200

    relogin = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": email, "password": NEW_PASSWORD}),
        content_type="application/json",
    )
    assert relogin.status_code == 200

    user = User.objects.get(email=email)
    assert user.is_active is True
    assert user.check_password(NEW_PASSWORD)
