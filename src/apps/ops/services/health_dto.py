"""Ops health check DTOs, timeouts, and overall_status matrix (Observability V1).

Health checks are **read-only**: no DB/Redis/provider mutations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

HealthStatus = Literal["healthy", "degraded", "unhealthy", "unknown"]
HealthReason = Literal[
    "ok",
    "timeout",
    "disabled",
    "authentication",
    "dns",
    "connection",
    "unknown",
]
HealthSource = Literal["live", "cached", "config", "heartbeat", "derived"]
MetricStatus = Literal["ok", "warning", "critical", "unknown"]

HEALTH_SCHEMA_VERSION = 1

# Per-check timeout budgets (ms) — do not invent ad-hoc timeouts in checkers.
TIMEOUT_MS = {
    "database": 100,
    "redis": 100,
    "celery_worker": 1000,
    "celery_beat": 250,
    "polygon_rpc": 500,
    "airalo": 100,
    "walletconnect": 100,
    "api": 50,
}

# In-process probe caches (no Redis writes — read-only guardrail).
POLYGON_CACHE_TTL_SECONDS = 45
CELERY_WORKER_CACHE_TTL_SECONDS = 20


@dataclass(slots=True)
class CacheMeta:
    hit: bool
    ttl_remaining_ms: int | None = None
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HealthCheck:
    status: HealthStatus
    reason: HealthReason
    message: str
    checked_at: str
    source: HealthSource
    timeout_ms: int
    latency_ms: int | None = None
    last_success_at: str | None = None
    cache: CacheMeta | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "status": self.status,
            "reason": self.reason,
            "message": self.message,
            "checked_at": self.checked_at,
            "source": self.source,
            "timeout_ms": self.timeout_ms,
            "latency_ms": self.latency_ms,
            "last_success_at": self.last_success_at,
            "details": self.details or {},
        }
        if self.cache is not None:
            data["cache"] = self.cache.to_dict()
        return data


@dataclass(slots=True)
class HealthMetric:
    key: str
    value: float | int | None
    unit: str
    status: MetricStatus

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def iso_now(moment: datetime | None = None) -> str:
    from django.utils import timezone

    moment = moment or timezone.now()
    return moment.isoformat().replace("+00:00", "Z")


def compute_overall_status(checks: dict[str, HealthCheck]) -> HealthStatus:
    """Deterministic overall_status matrix (Observability V1 locked rules)."""
    critical = ("database", "redis")
    for name in critical:
        check = checks.get(name)
        if check is not None and check.status == "unhealthy":
            return "unhealthy"

    for name, check in checks.items():
        if name in critical:
            continue
        # Config-disabled providers stay healthy — do not degrade overall.
        if check.reason == "disabled" and check.status == "healthy":
            continue
        if check.status in ("degraded", "unhealthy"):
            return "degraded"

    for check in checks.values():
        if check.reason == "disabled" and check.status == "healthy":
            continue
        if check.status == "unknown":
            return "unknown"

    return "healthy"
