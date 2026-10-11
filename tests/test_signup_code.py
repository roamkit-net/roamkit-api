"""Mobile signup code: password rules, mail, and activation."""

import json
import re
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from apps.accounts.models import SignupCode
from apps.accounts.services.signup import (
    GENERIC_SIGNUP_MESSAGE,
    INVALID_CODE_MESSAGE,
    MAIL_DAY_MAX,
    MAX_ATTEMPTS,
    claim_signup_mail,
    clear_signup_mail_gap,
)

User = get_user_model()

PASSWORD = "SecurePass1!"
SIGNUP = {
    "first_name": "Ante",
    "last_name": "Vrcan",
    "email": "ante@example.com",
    "password": PASSWORD,
}


def _post(client: Client, path: str, payload: dict) -> object:
    return client.post(
        path,
        data=json.dumps(payload),
        content_type="application/json",
    )


def _code_from_mail() -> str:
    match = re.search(r"Code: (\d{4})", mail.outbox[-1].body)
    assert match is not None
    return match.group(1)


@pytest.fixture(autouse=True)
def _clear_signup_cache() -> None:
    cache.clear()


@pytest.mark.django_db
def test_signup_creates_inactive_user_and_mails_code(client: Client) -> None:
    response = _post(client, "/api/v1/auth/signup/", SIGNUP)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_SIGNUP_MESSAGE}
    user = User.objects.get(email="ante@example.com")
    assert user.is_active is False
    assert user.display_name == "Ante Vrcan"
    assert user.check_password(PASSWORD)
    assert user.signup_code.code_hash
    assert len(mail.outbox) == 1
    assert "Code: " in mail.outbox[0].body
    assert user.signup_code.code_hash not in mail.outbox[0].body


@pytest.mark.django_db
def test_signup_rejects_weak_password(client: Client) -> None:
    payload = {**SIGNUP, "password": "noupper1!"}
    response = _post(client, "/api/v1/auth/signup/", payload)

    assert response.status_code == 400
    assert "password" in response.json()
    assert User.objects.filter(email="ante@example.com").count() == 0
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_signup_active_email_stays_generic(client: Client) -> None:
    user = User.objects.create_user(email="ante@example.com", password="OldPass1!")
    response = _post(client, "/api/v1/auth/signup/", SIGNUP)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_SIGNUP_MESSAGE}
    user.refresh_from_db()
    assert user.is_active is True
    assert user.check_password("OldPass1!")
    assert user.display_name != "Ante Vrcan"
    assert len(mail.outbox) == 1
    assert "already exists" in mail.outbox[0].body
    assert "/login" in mail.outbox[0].body
    assert "/forgot-password" in mail.outbox[0].body
    assert "Code: " not in mail.outbox[0].body
    assert not SignupCode.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_confirm_activates_and_returns_tokens(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    code = _code_from_mail()

    response = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": code},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["access"]
    assert body["refresh"]
    user = User.objects.get(email="ante@example.com")
    assert user.is_active is True
    assert user.last_login_provider == User.LastLoginProvider.PASSWORD
    assert not SignupCode.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_confirm_wrong_code_stays_inactive(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)

    response = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": "0000"},
    )

    assert response.status_code == 400
    user = User.objects.get(email="ante@example.com")
    assert user.is_active is False
    assert user.signup_code.attempt_count == 1


@pytest.mark.django_db
def test_confirm_locks_after_too_many_attempts(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    code = _code_from_mail()
    for _ in range(MAX_ATTEMPTS):
        failed = _post(
            client,
            "/api/v1/auth/signup/confirm/",
            {"email": "ante@example.com", "code": "0000"},
        )
        assert failed.status_code == 400

    response = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": code},
    )

    assert response.status_code == 400
    assert INVALID_CODE_MESSAGE in response.content.decode()
    assert User.objects.get(email="ante@example.com").is_active is False


@pytest.mark.django_db
def test_confirm_rejects_expired_code(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    code = _code_from_mail()
    signup_code = SignupCode.objects.get(user__email="ante@example.com")
    signup_code.expires_at = timezone.now() - timedelta(seconds=1)
    signup_code.save(update_fields=["expires_at"])

    response = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": code},
    )

    assert response.status_code == 400
    assert INVALID_CODE_MESSAGE in response.content.decode()
    assert User.objects.get(email="ante@example.com").is_active is False


@pytest.mark.django_db
def test_resend_replaces_the_code(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    first = _code_from_mail()
    clear_signup_mail_gap("ante@example.com")

    response = _post(
        client,
        "/api/v1/auth/signup/resend/",
        {"email": "ante@example.com"},
    )

    assert response.status_code == 200
    assert len(mail.outbox) == 2
    second = _code_from_mail()
    assert second != first
    old = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": first},
    )
    if first != second:
        assert old.status_code == 400
    confirmed = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": second},
    )
    assert confirmed.status_code == 200


@pytest.mark.django_db
def test_resend_unknown_email_sends_nothing(client: Client) -> None:
    response = _post(
        client,
        "/api/v1/auth/signup/resend/",
        {"email": "missing@example.com"},
    )

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_SIGNUP_MESSAGE}
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_register_link_flow_is_unchanged(client: Client) -> None:
    response = _post(
        client,
        "/api/v1/auth/register/",
        {"email": "web@example.com"},
    )

    assert response.status_code == 200
    assert "set-password" in mail.outbox[0].body
    assert "Code: " not in mail.outbox[0].body


@pytest.mark.django_db
def test_signup_missing_fields_stay_400(client: Client) -> None:
    response = _post(client, "/api/v1/auth/signup/", {})

    assert response.status_code == 400
    assert response.json() != {"detail": GENERIC_SIGNUP_MESSAGE}
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_signup_outcomes_share_the_same_body(client: Client) -> None:
    created = _post(client, "/api/v1/auth/signup/", SIGNUP)
    User.objects.create_user(email="active@example.com", password="OldPass1!")
    active = _post(
        client,
        "/api/v1/auth/signup/",
        {**SIGNUP, "email": "active@example.com"},
    )
    unknown = _post(
        client,
        "/api/v1/auth/signup/resend/",
        {"email": "missing@example.com"},
    )

    assert created.json() == active.json() == unknown.json()
    assert created.json()["detail"] == GENERIC_SIGNUP_MESSAGE


@pytest.mark.django_db
def test_repeat_signup_refreshes_pending_password_after_gap(
    client: Client,
) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    first = _code_from_mail()
    clear_signup_mail_gap("ante@example.com")

    response = _post(
        client,
        "/api/v1/auth/signup/",
        {
            **SIGNUP,
            "first_name": "Iva",
            "last_name": "Horvat",
            "password": "OtherPass2!",
        },
    )

    assert response.status_code == 200
    user = User.objects.get(email="ante@example.com")
    assert user.is_active is False
    assert user.display_name == "Iva Horvat"
    assert user.check_password("OtherPass2!")
    assert not user.check_password(PASSWORD)
    second = _code_from_mail()
    assert second != first
    old = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": first},
    )
    assert old.status_code == 400
    assert user.is_active is False
    confirmed = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": second},
    )
    assert confirmed.status_code == 200


@pytest.mark.django_db
def test_repeat_signup_inside_gap_changes_nothing(client: Client) -> None:
    _post(client, "/api/v1/auth/signup/", SIGNUP)
    first_hash = SignupCode.objects.get(user__email="ante@example.com").code_hash

    response = _post(
        client,
        "/api/v1/auth/signup/",
        {**SIGNUP, "first_name": "Iva", "password": "OtherPass2!"},
    )

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_SIGNUP_MESSAGE}
    user = User.objects.get(email="ante@example.com")
    assert user.display_name == "Ante Vrcan"
    assert user.check_password(PASSWORD)
    assert user.signup_code.code_hash == first_hash
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_daily_mail_cap_blocks_another_code(client: Client) -> None:
    assert claim_signup_mail("ante@example.com")
    for _ in range(MAIL_DAY_MAX - 1):
        clear_signup_mail_gap("ante@example.com")
        assert claim_signup_mail("ante@example.com")
    clear_signup_mail_gap("ante@example.com")

    response = _post(client, "/api/v1/auth/signup/", SIGNUP)

    assert response.status_code == 200
    assert response.json() == {"detail": GENERIC_SIGNUP_MESSAGE}
    assert User.objects.filter(email="ante@example.com").count() == 0
    assert len(mail.outbox) == 0


@override_settings(TURNSTILE_ENABLED=True)
@pytest.mark.django_db
def test_mobile_signup_ignores_turnstile(client: Client) -> None:
    started = _post(client, "/api/v1/auth/signup/", SIGNUP)
    assert started.status_code == 200
    code = _code_from_mail()
    clear_signup_mail_gap("ante@example.com")
    resent = _post(
        client,
        "/api/v1/auth/signup/resend/",
        {"email": "ante@example.com"},
    )
    assert resent.status_code == 200
    confirmed = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": code},
    )
    assert confirmed.status_code == 400
    latest = _code_from_mail()
    ok = _post(
        client,
        "/api/v1/auth/signup/confirm/",
        {"email": "ante@example.com", "code": latest},
    )
    assert ok.status_code == 200
    assert "turnstile_token" not in ok.json()


@override_settings(TURNSTILE_ENABLED=True)
@pytest.mark.django_db
def test_web_register_still_requires_turnstile(client: Client) -> None:
    response = _post(
        client,
        "/api/v1/auth/register/",
        {"email": "web@example.com"},
    )

    assert response.status_code == 400
    assert "turnstile_token" in response.json()
    assert len(mail.outbox) == 0
