"""User registration and activation services."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from apps.accounts.services.email import (
    send_activation_email,
    send_password_reset_email,
)
from apps.accounts.tokens import account_activation_token
from apps.billing.services import ensure_billing_account

User = get_user_model()

GENERIC_REGISTER_MESSAGE = (
    "If this email can be registered, you will receive a confirmation link shortly."
)


class RegistrationError(Exception):
    """Raised when registration or activation cannot complete."""

    def __init__(self, message: str, code: str = "registration_failed") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


class ActivationError(Exception):
    """Raised when activation fails (invalid token, user, or password)."""

    def __init__(
        self,
        message: str,
        code: str = "activation_failed",
        *,
        field: str | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.field = field
        super().__init__(message)


def register_user(*, email: str) -> None:
    """Start email-only registration: create pending user or resend mail.

    Always succeeds from the caller's perspective (no email enumeration).
    """
    normalized = User.objects.normalize_email(email)
    with transaction.atomic():
        user = User.objects.select_for_update().filter(email=normalized).first()
        if user is None:
            user = User(email=normalized, is_active=False)
            user.set_unusable_password()
            try:
                user.save()
                ensure_billing_account(user)
            except IntegrityError:
                # Race: another request created the same email.
                user = User.objects.filter(email=normalized).first()
                if user is None:
                    return
                if user.is_active:
                    if user.has_usable_password():
                        send_password_reset_email(user)
                    return
                send_activation_email(user)
                return
            else:
                send_activation_email(user)
                return

        if user.is_active:
            # Already registered — send reset mail instead of silence so the
            # "check your email" UX still works for returning users.
            if user.has_usable_password():
                send_password_reset_email(user)
            return

        send_activation_email(user)


def decode_uid(uid: str) -> int | None:
    try:
        return int(force_str(urlsafe_base64_decode(uid)))
    except (TypeError, ValueError, OverflowError):
        return None


def activate_user(
    *,
    uid: str,
    token: str,
    password: str,
    password_confirm: str,
) -> User:
    """Validate activation token, set password, and activate the user."""
    if password != password_confirm:
        raise ActivationError(
            "Passwords do not match.",
            code="password_mismatch",
            field="password_confirm",
        )

    try:
        validate_password(password)
    except DjangoValidationError as exc:
        raise ActivationError(
            " ".join(exc.messages),
            code="password_invalid",
            field="password",
        ) from exc

    user_id = decode_uid(uid)
    if user_id is None:
        raise ActivationError(
            "Invalid or expired activation link.",
            code="invalid_link",
        )

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist as exc:
        raise ActivationError(
            "Invalid or expired activation link.",
            code="invalid_link",
        ) from exc

    if user.is_active and user.has_usable_password():
        raise ActivationError(
            "This account is already activated.",
            code="already_active",
        )

    if not account_activation_token.check_token(user, token):
        raise ActivationError(
            "Invalid or expired activation link.",
            code="invalid_token",
        )

    user.set_password(password)
    user.is_active = True
    user.save(update_fields=["password", "is_active", "updated_at"])
    return user
