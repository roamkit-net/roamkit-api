"""Thin counter helper — Sentry metrics when available, always structured logs."""

from __future__ import annotations

import logging

logger = logging.getLogger("roamkit.metrics")


def incr(name: str, value: int = 1, **tags: str) -> None:
    """Increment a named counter."""
    extra = {"metric": name, "metric_value": value, **tags}
    logger.info("metric=%s value=%s", name, value, extra=extra)
    try:
        import sentry_sdk

        attrs = dict(tags) if tags else None
        sentry_sdk.metrics.count(name, value, attributes=attrs)
    except Exception:  # noqa: BLE001
        logger.debug("sentry metrics unavailable for %s", name, exc_info=True)


def observe(name: str, value: float, **tags: str) -> None:
    """Record a distribution / timing sample (seconds or other units)."""
    extra = {"metric": name, "metric_value": value, **tags}
    logger.info("metric=%s value=%s", name, value, extra=extra)
    try:
        import sentry_sdk

        attrs = dict(tags) if tags else None
        sentry_sdk.metrics.distribution(name, value, attributes=attrs)
    except Exception:  # noqa: BLE001
        logger.debug("sentry metrics unavailable for %s", name, exc_info=True)
