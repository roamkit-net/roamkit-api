"""Health check endpoints for load balancers and deploy scripts."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection
from django.db.utils import DatabaseError, OperationalError
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis import Redis
from redis.exceptions import RedisError

TURNSTILE_HOSTNAME = "challenges.cloudflare.com"


def _check_database() -> tuple[bool, str]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except (DatabaseError, OperationalError) as exc:
        return False, str(exc)
    return True, "ok"


def _check_redis() -> tuple[bool, str]:
    try:
        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2)
        client.ping()
    except RedisError as exc:
        return False, str(exc)
    return True, "ok"


def _turnstile_hostname() -> str:
    url = getattr(
        settings,
        "TURNSTILE_VERIFY_URL",
        f"https://{TURNSTILE_HOSTNAME}/turnstile/v0/siteverify",
    )
    host = urlparse(url).hostname
    return host or TURNSTILE_HOSTNAME


def _hostname_resolvable(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, 443)
    except OSError:
        return False
    return True


@require_GET
def live(_request):
    """Process is running — used by Docker and Traefik liveness probes."""
    return JsonResponse({"status": "ok"})


@require_GET
def ready(_request):
    """Dependencies are available — used before routing traffic."""
    checks: dict[str, dict[str, str]] = {}

    db_ok, db_detail = _check_database()
    checks["database"] = {"status": "ok" if db_ok else "error", "detail": db_detail}

    redis_ok, redis_detail = _check_redis()
    checks["redis"] = {"status": "ok" if redis_ok else "error", "detail": redis_detail}

    all_ok = db_ok and redis_ok
    status_code = 200 if all_ok else 503
    payload = {"status": "ok" if all_ok else "degraded", "checks": checks}
    return JsonResponse(payload, status=status_code)


@require_GET
def turnstile(_request):
    """
    Turnstile config diagnostic — not used by Docker/Traefik probes.

    Does not call siteverify; only checks flag, secret presence, and DNS.
    """
    enabled = bool(getattr(settings, "TURNSTILE_ENABLED", False))
    secret_configured = bool(
        (getattr(settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
    )
    hostname = _turnstile_hostname()
    resolvable = _hostname_resolvable(hostname)

    payload = {
        "enabled": enabled,
        "secret_configured": secret_configured,
        "hostname": hostname,
        "hostname_resolvable": resolvable,
    }

    if not enabled:
        payload["status"] = "ok"
        return JsonResponse(payload, status=200)

    ok = secret_configured and resolvable
    payload["status"] = "ok" if ok else "misconfigured"
    return JsonResponse(payload, status=200 if ok else 503)


@require_GET
def google_oauth(_request):
    """
    Google OAuth config diagnostic — not used by Docker/Traefik probes.

    Does not call Google; only checks flag and client id presence.
    """
    enabled = bool(getattr(settings, "GOOGLE_OAUTH_ENABLED", False))
    client_id_configured = bool(
        (getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") or "").strip()
    )
    payload = {
        "enabled": enabled,
        "client_id_configured": client_id_configured,
    }
    if not enabled:
        payload["status"] = "ok"
        return JsonResponse(payload, status=200)

    ok = client_id_configured
    payload["status"] = "ok" if ok else "misconfigured"
    return JsonResponse(payload, status=200 if ok else 503)
