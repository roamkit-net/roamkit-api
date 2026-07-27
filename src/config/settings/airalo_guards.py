"""Airalo settings guards — config presence/consistency only (not credential auth)."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured


def parse_blocked_client_ids(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def validate_staging_airalo(
    *,
    airalo_sandbox: bool,
    airalo_enabled: bool,
    client_id: str,
    client_secret: str,
    blocked_client_ids: frozenset[str] | set[str] | list[str],
) -> None:
    """Raise if staging Airalo config is inconsistent or uses a blocked live id."""
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    blocked = frozenset(blocked_client_ids)

    if airalo_sandbox is False:
        raise ImproperlyConfigured(
            "AIRALO_SANDBOX must be true in staging "
            "(staging must not use Fine Star live mode)."
        )

    if cid and cid in blocked:
        raise ImproperlyConfigured(
            "AIRALO_CLIENT_ID is blocked on staging "
            "(production Fine Star credentials are not allowed)."
        )

    if airalo_enabled and (not cid or not secret):
        raise ImproperlyConfigured(
            "AIRALO_CLIENT_ID and AIRALO_CLIENT_SECRET must both be set "
            "when AIRALO_ENABLED=true in staging."
        )


def validate_production_airalo(
    *,
    airalo_sandbox: bool,
    airalo_enabled: bool,
    client_id: str,
    client_secret: str,
) -> None:
    """Raise if production Airalo config is inconsistent.

    Checks presence and flag consistency only — does not authenticate
    against Airalo (that belongs to preflight / live smoke).
    """
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()

    if airalo_sandbox is True:
        raise ImproperlyConfigured(
            "AIRALO_SANDBOX must be false in production "
            "(Fine Star live Partner API)."
        )

    if airalo_enabled is not True:
        raise ImproperlyConfigured("AIRALO_ENABLED must be true in production.")

    if not cid or not secret:
        raise ImproperlyConfigured(
            "AIRALO_CLIENT_ID and AIRALO_CLIENT_SECRET must both be configured "
            "in production."
        )
