"""Observability V1 — ops health overall_status matrix and endpoint tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from apps.ops.services.health import build_ops_health, clear_probe_caches
from apps.ops.services.health_dto import (
    TIMEOUT_MS,
    HealthCheck,
    compute_overall_status,
    iso_now,
)

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def user(db) -> User:
    return User.objects.create_user(email="member@example.com", password=PASSWORD)


@pytest.fixture
def staff_user(db) -> User:
    return User.objects.create_user(
        email="ops@example.com",
        password=PASSWORD,
        is_staff=True,
    )


def _auth(client: Client, person: User) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": person.email, "password": PASSWORD}),
        content_type="application/json",
    )
    assert response.status_code == 200
    return {"HTTP_AUTHORIZATION": f"Bearer {response.json()['access']}"}


def _check(
    status: str,
    *,
    reason: str = "ok",
    name: str = "x",
) -> HealthCheck:
    now = iso_now()
    return HealthCheck(
        status=status,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        message=name,
        checked_at=now,
        source="live",
        timeout_ms=100,
    )


def test_overall_status_db_unhealthy() -> None:
    checks = {
        "database": _check("unhealthy", reason="connection"),
        "redis": _check("healthy"),
        "celery_worker": _check("healthy"),
    }
    assert compute_overall_status(checks) == "unhealthy"


def test_overall_status_redis_unhealthy() -> None:
    checks = {
        "database": _check("healthy"),
        "redis": _check("unhealthy", reason="connection"),
    }
    assert compute_overall_status(checks) == "unhealthy"


def test_overall_status_celery_degraded() -> None:
    checks = {
        "database": _check("healthy"),
        "redis": _check("healthy"),
        "celery_worker": _check("degraded", reason="connection"),
    }
    assert compute_overall_status(checks) == "degraded"


def test_overall_status_disabled_providers_stay_healthy() -> None:
    checks = {
        "database": _check("healthy"),
        "redis": _check("healthy"),
        "airalo": _check("healthy", reason="disabled"),
        "walletconnect": _check("healthy", reason="disabled"),
        "celery_beat": _check("unknown", reason="unknown"),
    }
    # beat unknown → overall unknown (nothing worse)
    assert compute_overall_status(checks) == "unknown"


def test_overall_status_all_healthy() -> None:
    checks = {
        "database": _check("healthy"),
        "redis": _check("healthy"),
        "celery_worker": _check("healthy"),
        "airalo": _check("healthy", reason="disabled"),
    }
    assert compute_overall_status(checks) == "healthy"


def test_overall_status_polygon_timeout_degraded() -> None:
    checks = {
        "database": _check("healthy"),
        "redis": _check("healthy"),
        "polygon_rpc": _check("degraded", reason="timeout"),
    }
    assert compute_overall_status(checks) == "degraded"


@pytest.mark.django_db
def test_ops_health_auth_matrix(client: Client, user: User, staff_user: User) -> None:
    assert client.get("/api/v1/admin/health/").status_code == 401
    assert client.get("/api/v1/admin/health/", **_auth(client, user)).status_code == 403
    ok = client.get("/api/v1/admin/health/", **_auth(client, staff_user))
    assert ok.status_code == 200
    assert ok["Cache-Control"] == "no-store"
    body = ok.json()
    assert body["schema_version"] == 1
    assert body["overall_status"] in {
        "healthy",
        "degraded",
        "unhealthy",
        "unknown",
    }
    assert "generated_at" in body
    assert "version" in body
    assert "release" in body["version"]
    assert "deployment_id" in body["version"]
    assert "dependencies" in body
    assert "workers" in body
    assert "providers" in body
    assert "metrics" in body
    db = body["dependencies"]["database"]
    assert set(db.keys()) >= {
        "status",
        "reason",
        "message",
        "checked_at",
        "source",
        "timeout_ms",
        "details",
    }
    wc = body["providers"]["walletconnect"]
    assert wc["source"] == "config"
    assert wc["reason"] in {"ok", "disabled"}


@pytest.mark.django_db
def test_ops_health_query_budget() -> None:
    # DoD counts builder queries only (JWT auth may add a User SELECT on HTTP).
    with CaptureQueriesContext(connection) as ctx:
        build_ops_health()
    assert len(ctx) <= 5, f"queries={len(ctx)} {[q['sql'][:80] for q in ctx]}"


@pytest.mark.django_db
def test_live_ready_still_compatible(client: Client) -> None:
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    ready = client.get("/health/ready")
    assert ready.status_code in (200, 503)
    payload = ready.json()
    assert "status" in payload
    assert "checks" in payload


@pytest.fixture(autouse=True)
def _reset_probe_caches() -> None:
    clear_probe_caches()
    yield
    clear_probe_caches()


def test_celery_worker_timeout_budget() -> None:
    assert TIMEOUT_MS["celery_worker"] == 1000


@pytest.mark.django_db
def test_celery_worker_probe_uses_in_process_cache() -> None:
    ping = {"celery@worker": {"ok": "pong"}}
    inspector = MagicMock()
    inspector.ping.return_value = ping
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspector

    with patch("config.celery.app", celery_app):
        from apps.ops.services.health import _check_celery_worker

        first = _check_celery_worker()
        second = _check_celery_worker()

    assert first.status == "healthy"
    assert first.source == "live"
    assert first.cache is not None and first.cache.hit is False
    assert first.timeout_ms == 1000

    assert second.status == "healthy"
    assert second.source == "cached"
    assert second.cache is not None and second.cache.hit is True
    assert inspector.ping.call_count == 1
    celery_app.control.inspect.assert_called_once()
    assert celery_app.control.inspect.call_args.kwargs["timeout"] == 1.0


@pytest.mark.django_db
@patch("apps.ops.services.health._check_celery_worker")
@patch("apps.ops.services.health._check_polygon_rpc")
def test_build_ops_health_wc_disabled_does_not_unhealth(
    mock_poly: MagicMock,
    mock_celery: MagicMock,
    settings,
) -> None:
    settings.WALLETCONNECT_ENABLED = False
    settings.AIRALO_ENABLED = False
    now = iso_now()
    mock_celery.return_value = HealthCheck(
        status="healthy",
        reason="ok",
        message="ok",
        checked_at=now,
        source="cached",
        timeout_ms=1000,
    )
    mock_poly.return_value = HealthCheck(
        status="healthy",
        reason="ok",
        message="ok",
        checked_at=now,
        source="cached",
        timeout_ms=500,
    )
    # Force beat unknown only — with disabled providers healthy.
    payload = build_ops_health()
    assert payload["providers"]["walletconnect"]["reason"] == "disabled"
    assert payload["providers"]["walletconnect"]["status"] == "healthy"
    assert payload["providers"]["airalo"]["reason"] == "disabled"
    assert payload["providers"]["airalo"]["status"] == "healthy"
    assert payload["overall_status"] in {"healthy", "unknown", "degraded"}
