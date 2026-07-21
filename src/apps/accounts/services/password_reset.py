"""Password reset services."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError

from apps.accounts.services.email import send_password_reset_email
from apps.accounts.services.registration import decode_uid
from apps.accounts.tokens import password_reset_token

User = get_user_model()

GENERIC_PASSWORD_RESET_MESSAGE = (
    "If an account exists for this email, "
    "you will receive a password reset link shortly."
)


class PasswordResetError(Exception):
    """Raised when password reset confirmation fails."""

    def __init__(
        self,
        message: str,
        code: str = "password_reset_failed",
        *,
        field: str | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.field = field
        super().__init__(message)


def request_password_reset(*, email: str) -> None:
    """Send a reset email when an active user with a usable password exists."""
    normalized = User.objects.normalize_email(email)
    user = User.objects.filter(email=normalized, is_active=True).first()
    if user is None or not user.has_usable_password():
        return
    send_password_reset_email(user)


def confirm_password_reset(
    *,
    uid: str,
    token: str,
    password: str,
    password_confirm: str,
) -> User:
    """Validate reset token and set a new password."""
    if password != password_confirm:
        raise PasswordResetError(
            "Passwords do not match.",
            code="password_mismatch",
            field="password_confirm",
        )

    try:
        validate_password(password)
    except DjangoValidationError as exc:
        raise PasswordResetError(
            " ".join(exc.messages),
            code="password_invalid",
            field="password",
        ) from exc

    user_id = decode_uid(uid)
    if user_id is None:
        raise PasswordResetError(
            "Invalid or expired reset link.",
            code="invalid_link",
        )

    try:
        user = User.objects.get(pk=user_id, is_active=True)
    except User.DoesNotExist as exc:
        raise PasswordResetError(
            "Invalid or expired reset link.",
            code="invalid_link",
        ) from exc

    if not password_reset_token.check_token(user, token):
        raise PasswordResetError(
            "Invalid or expired reset link.",
            code="invalid_token",
        )

    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    return user
