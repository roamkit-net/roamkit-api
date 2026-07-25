"""Production secret validation helpers (safe to import from tests)."""

from django.core.exceptions import ImproperlyConfigured


def require_production_secret(secret: str | None) -> str:
    """Reject missing or placeholder Django secret keys."""
    value = (secret or "").strip()
    if not value or value.startswith("change-me"):
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set to a non-placeholder value in production."
        )
    return value
