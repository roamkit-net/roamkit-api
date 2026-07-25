"""Optional Sentry init for production / staging."""

from __future__ import annotations

import os


def init_sentry(*, environment: str) -> bool:
    """Initialize Sentry when ``SENTRY_DSN`` is set. Returns True if enabled."""
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=environment or "unknown",
        integrations=[DjangoIntegration(), CeleryIntegration()],
        traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        send_default_pii=False,
    )
    return True
