"""Health check endpoints for load balancers and deploy scripts."""

from django.conf import settings
from django.db import connection
from django.db.utils import DatabaseError, OperationalError
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis import Redis
from redis.exceptions import RedisError


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
