"""Fail-open gate when Turnstile siteverify is unavailable."""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

from core import metrics

logger = logging.getLogger(__name__)


def _parse_rate(rate: str) -> tuple[int, int]:
    """Parse DRF-style ``N/period`` into (num_requests, period_seconds)."""
    num_s, period = rate.split("/")
    num = int(num_s)
    period = period.strip().lower()
    seconds = {
        "s": 1,
        "sec": 1,
        "second": 1,
        "seconds": 1,
        "m": 60,
        "min": 60,
        "minute": 60,
        "minutes": 60,
        "h": 3600,
        "hour": 3600,
        "hours": 3600,
        "d": 86400,
        "day": 86400,
        "days": 86400,
    }.get(period)
    if seconds is None:
        raise ValueError(f"Unsupported rate period: {rate}")
    return num, seconds


def allow_degraded_request(*, remoteip: str, endpoint: str) -> bool:
    """
    Return True if the request may proceed under fail-open.

    Uses a stricter per-IP gate from ``AUTH_TURNSTILE_DEGRADED_RATE``.
    """
    rate = getattr(settings, "AUTH_TURNSTILE_DEGRADED_RATE", "5/hour")
    limit, period = _parse_rate(rate)
    window = int(time.time()) // period
    key = f"auth:turnstile_degraded:{remoteip}:{endpoint}:{window}"
    try:
        count = cache.incr(key)
    except ValueError:
        cache.add(key, 1, timeout=period)
        count = 1

    if count > limit:
        metrics.incr("turnstile_degraded_gate_block_total", endpoint=endpoint)
        logger.warning(
            "turnstile_degraded_gate_block ip_window=%s endpoint=%s count=%s limit=%s",
            window,
            endpoint,
            count,
            limit,
        )
        return False
    return True
