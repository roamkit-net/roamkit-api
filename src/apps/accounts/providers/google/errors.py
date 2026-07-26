"""Google OAuth error codes (ADR 015)."""

from __future__ import annotations

from django.db import models
from rest_framework import status
from rest_framework.exceptions import APIException


class GoogleAuthErrorCode(models.TextChoices):
    INVALID_TOKEN = "google_invalid_token", "google_invalid_token"
    EMAIL_NOT_VERIFIED = "google_email_not_verified", "google_email_not_verified"
    ACCOUNT_DISABLED = "google_account_disabled", "google_account_disabled"
    SUB_CONFLICT = "google_sub_conflict", "google_sub_conflict"
    FEATURE_DISABLED = "google_feature_disabled", "google_feature_disabled"
    VERIFY_UNAVAILABLE = "google_verify_unavailable", "google_verify_unavailable"


_HTTP: dict[str, int] = {
    GoogleAuthErrorCode.INVALID_TOKEN: status.HTTP_400_BAD_REQUEST,
    GoogleAuthErrorCode.EMAIL_NOT_VERIFIED: status.HTTP_400_BAD_REQUEST,
    GoogleAuthErrorCode.ACCOUNT_DISABLED: status.HTTP_401_UNAUTHORIZED,
    GoogleAuthErrorCode.SUB_CONFLICT: status.HTTP_409_CONFLICT,
    GoogleAuthErrorCode.FEATURE_DISABLED: status.HTTP_404_NOT_FOUND,
    GoogleAuthErrorCode.VERIFY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
}

_DETAIL: dict[str, str] = {
    GoogleAuthErrorCode.INVALID_TOKEN: "Invalid Google credential.",
    GoogleAuthErrorCode.EMAIL_NOT_VERIFIED: ("Google account email is not verified."),
    GoogleAuthErrorCode.ACCOUNT_DISABLED: "This account is disabled.",
    GoogleAuthErrorCode.SUB_CONFLICT: "Google account is already linked.",
    GoogleAuthErrorCode.FEATURE_DISABLED: "Not found.",
    GoogleAuthErrorCode.VERIFY_UNAVAILABLE: (
        "Google sign-in is temporarily unavailable. Please try again."
    ),
}


class GoogleAuthError(APIException):
    """Structured Google auth failure with locked ``code`` + HTTP status."""

    def __init__(self, code: GoogleAuthErrorCode | str) -> None:
        code_value = str(code)
        self.status_code = _HTTP[code_value]
        detail = {"code": code_value, "detail": _DETAIL[code_value]}
        super().__init__(detail=detail)
        self.detail = detail
