"""Google OAuth provider (GIS ID token)."""

from apps.accounts.providers.google.errors import GoogleAuthError, GoogleAuthErrorCode

__all__ = [
    "GoogleAuthError",
    "GoogleAuthErrorCode",
    "authenticate_with_google",
]


def __getattr__(name: str):
    if name == "authenticate_with_google":
        from apps.accounts.providers.google.service import authenticate_with_google

        return authenticate_with_google
    raise AttributeError(name)
