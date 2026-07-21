"""User registration service."""

from django.contrib.auth import get_user_model
from django.db import IntegrityError

User = get_user_model()


class RegistrationError(Exception):
    """Raised when registration cannot complete."""

    def __init__(self, message: str, code: str = "registration_failed") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


def register_user(*, email: str, password: str) -> User:
    """Create a new user with the given email and password.

    Raises:
        RegistrationError: if email is already registered.
    """
    normalized = User.objects.normalize_email(email)
    try:
        return User.objects.create_user(email=normalized, password=password)
    except IntegrityError as exc:
        raise RegistrationError(
            "A user with this email already exists.",
            code="email_taken",
        ) from exc
