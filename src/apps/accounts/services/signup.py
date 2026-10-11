"""Mobile signup: collect a password up front and confirm with a 4-digit code."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import SignupCode
from apps.accounts.services.email import (
    send_signup_account_exists_email,
    send_signup_code_email,
)
from apps.billing.services import ensure_billing_account

User = get_user_model()

GENERIC_SIGNUP_MESSAGE = (
    "Check your email. If this address can be registered, we sent a "
    "verification code. If an account already exists, we sent sign-in "
    "instructions."
)
INVALID_CODE_MESSAGE = "Invalid or expired verification code."
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
MAIL_GAP_SECONDS = 10 * 60
MAIL_DAY_SECONDS = 24 * 60 * 60
MAIL_DAY_MAX = 10
PASSWORD_SYMBOLS = set("!$%^&*()_-+=[]{}|;:'\",.<>?/")


class SignupCodeError(Exception):
    """Raised when a verification code cannot be accepted."""

    def __init__(self, message: str = INVALID_CODE_MESSAGE) -> None:
        self.message = message
        super().__init__(message)


def password_rule_errors(password: str) -> list[str]:
    """Mobile signup rules. Web activation keeps the Django validators."""
    errors: list[str] = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if len(password) > 128:
        errors.append("Password must be at most 128 characters.")
    if not any(character.isupper() for character in password):
        errors.append("Password must include an uppercase letter.")
    if not any(character.islower() for character in password):
        errors.append("Password must include a lowercase letter.")
    if not any(character.isdigit() for character in password):
        errors.append("Password must include a number.")
    if not any(character in PASSWORD_SYMBOLS for character in password):
        errors.append("Password must include a symbol.")
    return errors


def display_name_for(*, first_name: str, last_name: str) -> str:
    return f"{first_name.strip()} {last_name.strip()}".strip()[:255]


def _new_code() -> str:
    return f"{secrets.randbelow(10000):04d}"


def _mail_digest(email: str) -> str:
    normalized = User.objects.normalize_email(email)
    return hashlib.sha256(normalized.encode()).hexdigest()


def claim_signup_mail(email: str) -> bool:
    """Atomically reserve one outbound signup mail for this address.

    At most one mail every 10 minutes and 10 mails in 24 hours. The claim
    uses cache.add so two workers cannot both win.
    """
    digest = _mail_digest(email)
    slot_key = None
    for index in range(MAIL_DAY_MAX):
        key = f"signup-mail:day:{digest}:{index}"
        if cache.add(key, 1, timeout=MAIL_DAY_SECONDS):
            slot_key = key
            break
    if slot_key is None:
        return False
    if cache.add(f"signup-mail:gap:{digest}", 1, timeout=MAIL_GAP_SECONDS):
        return True
    cache.delete(slot_key)
    return False


def clear_signup_mail_gap(email: str) -> None:
    """Drop the 10-minute gap. Tests use this to simulate a later request."""
    cache.delete(f"signup-mail:gap:{_mail_digest(email)}")


def _save_code(user: User, code: str) -> None:
    SignupCode.objects.update_or_create(
        user=user,
        defaults={
            "code_hash": make_password(code),
            "expires_at": timezone.now() + CODE_TTL,
            "attempt_count": 0,
        },
    )


def start_signup(
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str = "",
) -> None:
    """Create or refresh an inactive signup and mail a code.

    An active account gets an account-exists mail and no code. A repeat
    request inside the mail limit changes nothing. The public response is
    always the same.
    """
    normalized = User.objects.normalize_email(email)
    if not claim_signup_mail(normalized):
        return
    name = display_name_for(first_name=first_name, last_name=last_name)
    code = _new_code()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=normalized).first()
        if user is None:
            user = User(email=normalized, is_active=False, display_name=name)
            user.set_password(password)
            try:
                with transaction.atomic():
                    user.save()
            except IntegrityError:
                user = User.objects.select_for_update().get(email=normalized)
            else:
                ensure_billing_account(user)
                _save_code(user, code)
                send_signup_code_email(user, code)
                return

        if user.is_active:
            send_signup_account_exists_email(user)
            return

        user.display_name = name
        user.set_password(password)
        user.save(update_fields=["display_name", "password", "updated_at"])
        ensure_billing_account(user)
        _save_code(user, code)
        send_signup_code_email(user, code)


def resend_signup_code(*, email: str) -> None:
    """Mail a fresh code when a mobile signup is still pending.

    An active account may get the account-exists mail. An unknown address
    gets nothing. Neither path reveals itself in the HTTP response.
    """
    normalized = User.objects.normalize_email(email)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=normalized).first()
        if user is None:
            return
        if user.is_active:
            if claim_signup_mail(normalized):
                send_signup_account_exists_email(user)
            return
        if not SignupCode.objects.filter(user=user).exists():
            return
        if not claim_signup_mail(normalized):
            return
        code = _new_code()
        _save_code(user, code)
        send_signup_code_email(user, code)


def confirm_signup(*, email: str, code: str) -> User:
    """Activate the pending user when the code matches."""
    normalized = User.objects.normalize_email(email)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=normalized).first()
        if user is None or user.is_active:
            raise SignupCodeError()
        try:
            signup_code = SignupCode.objects.select_for_update().get(user=user)
        except SignupCode.DoesNotExist as exc:
            raise SignupCodeError() from exc
        if signup_code.attempt_count >= MAX_ATTEMPTS:
            raise SignupCodeError()
        if signup_code.expires_at <= timezone.now():
            raise SignupCodeError()
        if not check_password(code, signup_code.code_hash):
            signup_code.attempt_count += 1
            signup_code.save(update_fields=["attempt_count", "updated_at"])
        else:
            user.is_active = True
            user.save(update_fields=["is_active", "updated_at"])
            signup_code.delete()
            return user
    raise SignupCodeError()
