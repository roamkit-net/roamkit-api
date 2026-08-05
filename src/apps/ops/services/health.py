"""Read-only ops health aggregator (Observability V1).

Shared by ``GET /api/v1/admin/health`` and embedded ``dashboard.health``.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Count, Max, Q
from django.db.utils import DatabaseError, OperationalError
from django.utils import timezone
from redis import Redis
from redis.exceptions import RedisError

from apps.billing.models import DepositRequest
from apps.catalog.models import Package
from apps.esims.models import Esim
from apps.ops.services.health_dto import (
    HEALTH_SCHEMA_VERSION,
    POLYGON_CACHE_TTL_SECONDS,
    TIMEOUT_MS,
    CacheMeta,
    HealthCheck,
    HealthMetric,
    compute_overall_status,
    iso_now,
)
from apps.orders.models import Order

# Process-local Polygon probe cache (no Redis writes).
_polygon_cache: dict[str, Any] = {"expires_at": 0.0, "check": None}


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _check_api() -> HealthCheck:
    now = iso_now()
    return HealthCheck(
        status="healthy",
        reason="ok",
        message="API process responding",
        checked_at=now,
        source="live",
        timeout_ms=TIMEOUT_MS["api"],
        latency_ms=0,
        last_success_at=now,
        details={},
    )


def _check_database() -> HealthCheck:
    timeout_ms = TIMEOUT_MS["database"]
    started = time.perf_counter()
    checked = iso_now()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        latency = _elapsed_ms(started)
        return HealthCheck(
            status="healthy",
            reason="ok",
            message="database ok",
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=latency,
            last_success_at=checked,
            details={},
        )
    except (DatabaseError, OperationalError) as exc:
        return HealthCheck(
            status="unhealthy",
            reason="connection",
            message=str(exc)[:500],
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=_elapsed_ms(started),
            details={},
        )


def _check_redis() -> HealthCheck:
    timeout_ms = TIMEOUT_MS["redis"]
    started = time.perf_counter()
    checked = iso_now()
    try:
        client = Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=timeout_ms / 1000.0,
            socket_timeout=timeout_ms / 1000.0,
        )
        client.ping()
        latency = _elapsed_ms(started)
        return HealthCheck(
            status="healthy",
            reason="ok",
            message="redis ok",
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=latency,
            last_success_at=checked,
            details={},
        )
    except RedisError as exc:
        return HealthCheck(
            status="unhealthy",
            reason="connection",
            message=str(exc)[:500],
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=_elapsed_ms(started),
            details={},
        )


def _check_celery_worker() -> HealthCheck:
    timeout_ms = TIMEOUT_MS["celery_worker"]
    started = time.perf_counter()
    checked = iso_now()
    try:
        from config.celery import app as celery_app

        inspector = celery_app.control.inspect(timeout=timeout_ms / 1000.0)
        ping = inspector.ping() if inspector is not None else None
        latency = _elapsed_ms(started)
        if ping:
            return HealthCheck(
                status="healthy",
                reason="ok",
                message=f"celery workers responding ({len(ping)})",
                checked_at=checked,
                source="live",
                timeout_ms=timeout_ms,
                latency_ms=latency,
                last_success_at=checked,
                details={"workers": list(ping.keys())[:10]},
            )
        return HealthCheck(
            status="degraded",
            reason="connection",
            message="no celery workers answered ping",
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=latency,
            details={},
        )
    except Exception as exc:  # noqa: BLE001 — surface as degraded, never raise
        if _elapsed_ms(started) >= timeout_ms:
            reason = "timeout"
            status = "degraded"
        else:
            reason = "unknown"
            status = "unknown"
        return HealthCheck(
            status=status,  # type: ignore[arg-type]
            reason=reason,  # type: ignore[arg-type]
            message=str(exc)[:500],
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=_elapsed_ms(started),
            details={},
        )


def _check_celery_beat() -> HealthCheck:
    """Beat is not instrumented in V1 — honest unknown."""
    checked = iso_now()
    return HealthCheck(
        status="unknown",
        reason="unknown",
        message="celery beat heartbeat not instrumented",
        checked_at=checked,
        source="config",
        timeout_ms=TIMEOUT_MS["celery_beat"],
        details={},
    )


def _check_walletconnect() -> HealthCheck:
    checked = iso_now()
    enabled = bool(getattr(settings, "WALLETCONNECT_ENABLED", False))
    if enabled:
        return HealthCheck(
            status="healthy",
            reason="ok",
            message="WalletConnect enabled (config flag)",
            checked_at=checked,
            source="config",
            timeout_ms=TIMEOUT_MS["walletconnect"],
            last_success_at=checked,
            details={"enabled": True},
        )
    return HealthCheck(
        status="healthy",
        reason="disabled",
        message="WalletConnect disabled by configuration",
        checked_at=checked,
        source="config",
        timeout_ms=TIMEOUT_MS["walletconnect"],
        details={"enabled": False},
    )


def _check_airalo(*, last_synced_at: str | None) -> HealthCheck:
    checked = iso_now()
    enabled = bool(getattr(settings, "AIRALO_ENABLED", True))
    if not enabled:
        return HealthCheck(
            status="healthy",
            reason="disabled",
            message="Airalo disabled by configuration",
            checked_at=checked,
            source="config",
            timeout_ms=TIMEOUT_MS["airalo"],
            last_success_at=last_synced_at,
            details={"enabled": False},
        )
    client_id = (getattr(settings, "AIRALO_CLIENT_ID", "") or "").strip()
    client_secret = (getattr(settings, "AIRALO_CLIENT_SECRET", "") or "").strip()
    if not client_id or not client_secret:
        return HealthCheck(
            status="degraded",
            reason="authentication",
            message="Airalo enabled but credentials missing",
            checked_at=checked,
            source="config",
            timeout_ms=TIMEOUT_MS["airalo"],
            last_success_at=last_synced_at,
            details={"enabled": True, "credentials_configured": False},
        )
    return HealthCheck(
        status="healthy",
        reason="ok",
        message="Airalo enabled with credentials (no live partner probe)",
        checked_at=checked,
        source="derived" if last_synced_at else "config",
        timeout_ms=TIMEOUT_MS["airalo"],
        last_success_at=last_synced_at,
        details={"enabled": True, "credentials_configured": True},
    )


def _probe_polygon_live(timeout_ms: int) -> HealthCheck:
    checked = iso_now()
    rpc_url = (getattr(settings, "POLYGON_RPC_URL", "") or "").strip()
    if not rpc_url:
        return HealthCheck(
            status="unknown",
            reason="unknown",
            message="POLYGON_RPC_URL not configured",
            checked_at=checked,
            source="config",
            timeout_ms=timeout_ms,
            details={"configured": False},
        )
    started = time.perf_counter()
    try:
        from apps.integrations.polygon.client import PolygonRpcClient

        client = PolygonRpcClient(
            rpc_url=rpc_url,
            timeout=timeout_ms / 1000.0,
            retries=0,
            backoff_base=0,
        )
        block = client.call("eth_blockNumber", [])
        latency = _elapsed_ms(started)
        return HealthCheck(
            status="healthy",
            reason="ok",
            message="polygon RPC ok",
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=latency,
            last_success_at=checked,
            details={"block_number": block},
        )
    except Exception as exc:  # noqa: BLE001
        latency = _elapsed_ms(started)
        reason = "timeout" if latency >= timeout_ms else "connection"
        return HealthCheck(
            status="degraded",
            reason=reason,  # type: ignore[arg-type]
            message=str(exc)[:500],
            checked_at=checked,
            source="live",
            timeout_ms=timeout_ms,
            latency_ms=latency,
            details={"configured": True},
        )


def _check_polygon_rpc() -> HealthCheck:
    timeout_ms = TIMEOUT_MS["polygon_rpc"]
    now = time.time()
    cached = _polygon_cache.get("check")
    expires_at = float(_polygon_cache.get("expires_at") or 0.0)
    if cached is not None and now < expires_at:
        ttl_ms = int((expires_at - now) * 1000)
        check: HealthCheck = cached
        return HealthCheck(
            status=check.status,
            reason=check.reason,
            message=check.message,
            checked_at=iso_now(),
            source="cached",
            timeout_ms=timeout_ms,
            latency_ms=check.latency_ms,
            last_success_at=check.last_success_at,
            cache=CacheMeta(
                hit=True,
                ttl_remaining_ms=ttl_ms,
                expires_at=iso_now(timezone.now() + timedelta(milliseconds=ttl_ms)),
            ),
            details=dict(check.details or {}),
        )

    live = _probe_polygon_live(timeout_ms)
    _polygon_cache["check"] = live
    _polygon_cache["expires_at"] = now + POLYGON_CACHE_TTL_SECONDS
    # Return as cached-miss then subsequent hits are cached; first response is live.
    if live.source == "config":
        return live
    return HealthCheck(
        status=live.status,
        reason=live.reason,
        message=live.message,
        checked_at=live.checked_at,
        source="live",
        timeout_ms=timeout_ms,
        latency_ms=live.latency_ms,
        last_success_at=live.last_success_at,
        cache=CacheMeta(
            hit=False,
            ttl_remaining_ms=POLYGON_CACHE_TTL_SECONDS * 1000,
            expires_at=iso_now(
                timezone.now() + timedelta(seconds=POLYGON_CACHE_TTL_SECONDS)
            ),
        ),
        details=dict(live.details or {}),
    )


def _build_metrics(
    *,
    pending_deposits: int,
    stuck_installs: int,
    failed_orders_24h: int,
) -> list[HealthMetric]:
    def deposit_status(n: int) -> str:
        if n <= 0:
            return "ok"
        if n < 10:
            return "warning"
        return "critical"

    def stuck_status(n: int) -> str:
        if n <= 0:
            return "ok"
        if n < 20:
            return "warning"
        return "critical"

    def failed_status(n: int) -> str:
        if n <= 0:
            return "ok"
        if n < 5:
            return "warning"
        return "critical"

    return [
        HealthMetric(
            key="pending_deposits",
            value=pending_deposits,
            unit="count",
            status=deposit_status(pending_deposits),  # type: ignore[arg-type]
        ),
        HealthMetric(
            key="stuck_installs",
            value=stuck_installs,
            unit="count",
            status=stuck_status(stuck_installs),  # type: ignore[arg-type]
        ),
        HealthMetric(
            key="failed_orders_24h",
            value=failed_orders_24h,
            unit="count",
            status=failed_status(failed_orders_24h),  # type: ignore[arg-type]
        ),
        HealthMetric(
            key="queue_backlog",
            value=None,
            unit="jobs",
            status="unknown",
        ),
    ]


def _version_block() -> dict[str, str]:
    git_sha = getattr(settings, "ROAMKIT_GIT_SHA", "") or ""
    build_date = getattr(settings, "ROAMKIT_BUILD_DATE", "") or ""
    image_tag = getattr(settings, "ROAMKIT_IMAGE_TAG", "") or ""
    environment = getattr(settings, "ROAMKIT_ENVIRONMENT", "") or ""
    release = getattr(settings, "ROAMKIT_RELEASE", "") or image_tag or git_sha or ""
    deployment_id = getattr(settings, "ROAMKIT_DEPLOYMENT_ID", "") or (
        f"{image_tag}:{git_sha}" if image_tag or git_sha else ""
    )
    return {
        "git_sha": git_sha,
        "build_date": build_date,
        "image_tag": image_tag,
        "environment": environment,
        "release": release,
        "deployment_id": deployment_id,
    }


def build_ops_health() -> dict[str, Any]:
    """Aggregate all ops health checks (read-only; ≤5 DB queries)."""
    now = timezone.now()
    day_ago = now - timedelta(days=1)

    # Query 1: deposit + order aggregates
    deposit_agg = DepositRequest.objects.aggregate(
        pending=Count("id", filter=Q(status=DepositRequest.Status.PENDING)),
    )
    # Query 2: stuck installs
    stuck = Esim.objects.filter(
        status__in=(
            Esim.Status.PURCHASED,
            Esim.Status.INSTALLATION_STARTED,
        ),
        updated_at__lt=day_ago,
    ).count()
    # Query 3: failed orders 24h
    failed_orders = Order.objects.filter(
        status=Order.Status.FAILED,
        updated_at__gte=day_ago,
    ).count()
    # Query 4: last Airalo catalog sync (optional derived signal)
    last_sync = Package.objects.aggregate(m=Max("synced_at"))["m"]
    last_synced_at = last_sync.isoformat().replace("+00:00", "Z") if last_sync else None

    checks: dict[str, HealthCheck] = {
        "api": _check_api(),
        "database": _check_database(),  # query via SELECT 1 — counted in budget
        "redis": _check_redis(),
        "celery_worker": _check_celery_worker(),
        "celery_beat": _check_celery_beat(),
        "airalo": _check_airalo(last_synced_at=last_synced_at),
        "polygon_rpc": _check_polygon_rpc(),
        "walletconnect": _check_walletconnect(),
    }

    metrics = _build_metrics(
        pending_deposits=deposit_agg["pending"] or 0,
        stuck_installs=stuck,
        failed_orders_24h=failed_orders,
    )

    check_dicts = {name: check.to_dict() for name, check in checks.items()}
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "overall_status": compute_overall_status(checks),
        "generated_at": iso_now(now),
        "version": _version_block(),
        "dependencies": {
            "database": check_dicts["database"],
            "redis": check_dicts["redis"],
            "api": check_dicts["api"],
        },
        "workers": {
            "celery_worker": check_dicts["celery_worker"],
            "celery_beat": check_dicts["celery_beat"],
        },
        "providers": {
            "airalo": check_dicts["airalo"],
            "polygon_rpc": check_dicts["polygon_rpc"],
            "walletconnect": check_dicts["walletconnect"],
        },
        "metrics": [m.to_dict() for m in metrics],
        # Flat map for dashboard strip convenience (same DTOs).
        "checks": check_dicts,
    }
