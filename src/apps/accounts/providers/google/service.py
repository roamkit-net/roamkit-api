"""Google sign-in: resolve/link/create user and issue SimpleJWT."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from apps.accounts.providers.google.errors import GoogleAuthError, GoogleAuthErrorCode
from apps.accounts.providers.google.verify import GoogleIdentity, verify_google_id_token
from apps.billing.services import ensure_billing_account
from core import metrics
from shared.events.auth_events import (
    GoogleAccountCreated,
    GoogleAccountLinked,
    GoogleLoginConflict,
    GoogleLoginFailed,
    GoogleLoginSucceeded,
)
from shared.events.event_bus import event_bus

User = get_user_model()
logger = logging.getLogger("roamkit.auth.google")


class GoogleLoginOutcome(StrEnum):
    EXISTING = "existing"
    LINKED = "linked"
    CREATED = "created"


@dataclass(frozen=True, kw_only=True)
class GoogleLoginResult:
    access: str
    refresh: str
    user_id: int
    google_sub: str
    outcome: GoogleLoginOutcome


def _normalize_email(email: str) -> str:
    return User.objects.normalize_email(email.strip().lower())


def _log(
    *,
    result: str,
    reason: str = "",
    google_sub: str = "",
    user_id: int | None = None,
) -> None:
    extra: dict[str, object] = {
        "provider": "google",
        "result": result,
        "reason": reason,
    }
    if google_sub:
        extra["google_sub"] = google_sub
    if user_id is not None:
        extra["user_id"] = user_id
    logger.info(
        "google_auth result=%s reason=%s",
        result,
        reason or "-",
        extra=extra,
    )


def _fail(code: GoogleAuthErrorCode, *, google_sub: str = "") -> None:
    metrics.incr("google_login_failure_total", reason=code.value)
    event_bus.publish(
        GoogleLoginFailed(reason=code.value, google_sub=google_sub or None)
    )
    _log(result="failed", reason=code.value, google_sub=google_sub)
    raise GoogleAuthError(code)


def _issue_tokens(user: User) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


def _apply_profile(user: User, identity: GoogleIdentity) -> list[str]:
    fields: list[str] = []
    if identity.name and user.google_name != identity.name:
        user.google_name = identity.name
        fields.append("google_name")
    if identity.picture and user.google_picture != identity.picture:
        user.google_picture = identity.picture
        fields.append("google_picture")
    return fields


def _touch_login(user: User, identity: GoogleIdentity) -> None:
    update_fields = _apply_profile(user, identity)
    user.last_login_provider = User.LastLoginProvider.GOOGLE
    user.last_google_login_at = timezone.now()
    update_fields.extend(["last_login_provider", "last_google_login_at", "updated_at"])
    user.save(update_fields=list(dict.fromkeys(update_fields)))


def _link_or_activate(user: User, identity: GoogleIdentity) -> GoogleLoginOutcome:
    if user.google_sub and user.google_sub != identity.subject:
        metrics.incr("google_conflict_total")
        event_bus.publish(GoogleLoginConflict(google_sub=identity.subject))
        _fail(GoogleAuthErrorCode.SUB_CONFLICT, google_sub=identity.subject)

    if not user.is_active:
        if user.has_usable_password():
            _fail(
                GoogleAuthErrorCode.ACCOUNT_DISABLED,
                google_sub=identity.subject,
            )
        user.is_active = True

    outcome = GoogleLoginOutcome.EXISTING
    if not user.google_sub:
        user.google_sub = identity.subject
        outcome = GoogleLoginOutcome.LINKED

    update_fields = _apply_profile(user, identity)
    user.last_login_provider = User.LastLoginProvider.GOOGLE
    user.last_google_login_at = timezone.now()
    update_fields.extend(
        [
            "google_sub",
            "is_active",
            "last_login_provider",
            "last_google_login_at",
            "updated_at",
        ]
    )
    user.save(update_fields=list(dict.fromkeys(update_fields)))
    if outcome is GoogleLoginOutcome.LINKED:
        metrics.incr("google_auto_link_total")
        event_bus.publish(
            GoogleAccountLinked(user_id=user.pk, google_sub=identity.subject)
        )
    return outcome


def _resolve_user(
    identity: GoogleIdentity, *, normalized_email: str
) -> tuple[User, GoogleLoginOutcome]:
    with transaction.atomic():
        user = (
            User.objects.select_for_update().filter(google_sub=identity.subject).first()
        )
        if user is not None:
            if not user.is_active:
                _fail(
                    GoogleAuthErrorCode.ACCOUNT_DISABLED,
                    google_sub=identity.subject,
                )
            _touch_login(user, identity)
            return user, GoogleLoginOutcome.EXISTING

        user = User.objects.select_for_update().filter(email=normalized_email).first()
        if user is not None:
            return user, _link_or_activate(user, identity)

        user = User(
            email=normalized_email,
            google_sub=identity.subject,
            is_active=True,
            google_name=identity.name,
            google_picture=identity.picture,
            last_login_provider=User.LastLoginProvider.GOOGLE,
            last_google_login_at=timezone.now(),
        )
        user.set_unusable_password()
        user.save()
        ensure_billing_account(user)
        metrics.incr("google_new_user_total")
        event_bus.publish(
            GoogleAccountCreated(user_id=user.pk, google_sub=identity.subject)
        )
        return user, GoogleLoginOutcome.CREATED


def authenticate_with_google(*, credential: str) -> GoogleLoginResult:
    """Verify credential, link/create user under row lock, return JWT pair."""
    if not credential or not isinstance(credential, str) or not credential.strip():
        _fail(GoogleAuthErrorCode.INVALID_TOKEN)

    try:
        identity = verify_google_id_token(credential.strip())
    except GoogleAuthError as exc:
        code = GoogleAuthErrorCode(exc.detail["code"])
        if code != GoogleAuthErrorCode.FEATURE_DISABLED:
            metrics.incr("google_login_failure_total", reason=code.value)
            event_bus.publish(GoogleLoginFailed(reason=code.value, google_sub=None))
            _log(result="failed", reason=code.value)
        raise

    del credential
    normalized_email = _normalize_email(identity.email)

    user: User | None = None
    outcome = GoogleLoginOutcome.EXISTING
    for _attempt in range(3):
        try:
            user, outcome = _resolve_user(identity, normalized_email=normalized_email)
            break
        except IntegrityError:
            # Parallel create/link — retry a fresh atomic resolve.
            continue
    if user is None:
        _fail(GoogleAuthErrorCode.SUB_CONFLICT, google_sub=identity.subject)

    access, refresh = _issue_tokens(user)
    metrics.incr("google_login_success_total")
    event_bus.publish(
        GoogleLoginSucceeded(user_id=user.pk, google_sub=identity.subject)
    )
    _log(
        result="success",
        reason=outcome.value,
        google_sub=identity.subject,
        user_id=user.pk,
    )
    return GoogleLoginResult(
        access=access,
        refresh=refresh,
        user_id=user.pk,
        google_sub=identity.subject,
        outcome=outcome,
    )
