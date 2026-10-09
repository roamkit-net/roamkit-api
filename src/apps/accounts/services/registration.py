"""User registration and activation services."""

from __future__ import annotations

from enum import StrEnum

from django.conf import settings
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
from apps.billing.services.partner_invite_visit import validate_invite_visit
from apps.billing.services.partner_pending import unsign_partner_pending

User = get_user_model()

GENERIC_REGISTER_MESSAGE = (
    "If this email can be registered, you will receive a confirmation link shortly."
)


class RegistrationResult(StrEnum):
    """Service outcome. The view maps this; it is not an HTTP status."""

    CREATED = "created"
    EXISTING = "existing"
    ACCOUNT_EXISTS_FOR_INVITE = "account_exists_for_invite"


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


def register_user(
    *, email: str, partner_pending: str | None = None
) -> RegistrationResult:
    """Start email-only registration: create a pending user or resend mail.

    ``CREATED`` is only the request that inserted the user row.
    ``ACCOUNT_EXISTS_FOR_INVITE`` is only a verified invite for an active user.
    Every other existing account stays on the public anti-enumeration path.
    """
    normalized = User.objects.normalize_email(email)
    invite = _verified_invite_visit(partner_pending)
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
                    return RegistrationResult.EXISTING
                if user.is_active:
                    return _active_account_result(user, invite)
                send_activation_email(user)
                return RegistrationResult.EXISTING
            else:
                send_activation_email(user)
                _record_partner_pending(user, partner_pending)
                return RegistrationResult.CREATED

        if user.is_active:
            return _active_account_result(user, invite)

        send_activation_email(user)
        return RegistrationResult.EXISTING


def _verified_invite_visit(signed: str | None):
    """Visit that passed the existing signature and 30-day checks, or None.

    A missing, raw, expired, or forged value is not a verified invite.
    """
    if not signed or not settings.PARTNER_CHANNEL_ENABLED:
        return None
    payload = unsign_partner_pending(signed)
    if payload is None:
        return None
    return validate_invite_visit(
        payload["visit_id"],
        check_attribution_window=True,
    )


def _active_account_result(user: User, invite) -> RegistrationResult:
    """One result for an active account, including the insert race.

    A verified invite does not send mail. Public registration still sends
    the reset email when the account has a usable password.
    """
    if invite is not None:
        return RegistrationResult.ACCOUNT_EXISTS_FOR_INVITE
    if user.has_usable_password():
        send_password_reset_email(user)
    return RegistrationResult.EXISTING


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

    with transaction.atomic():
        user.set_password(password)
        user.is_active = True
        user.save(update_fields=["password", "is_active", "updated_at"])
        _apply_partner_pending(user)
    return user


def _record_partner_pending(user, signed: str | None) -> None:
    if not signed:
        return
    from apps.billing.services.partner_attribution import record_pending_for_new_user

    record_pending_for_new_user(user, signed)


def _apply_partner_pending(user) -> None:
    from apps.billing.services.partner_attribution import apply_pending_on_activation

    apply_pending_on_activation(user)
