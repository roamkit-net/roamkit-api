"""Tests for Turnstile / human verification on auth endpoints."""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.services.human_verification.base import HumanVerificationResult
from apps.accounts.services.human_verification.turnstile import (
    TurnstileVerificationService,
)

User = get_user_model()
PASSWORD = "SecurePass1!"


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.clear()


class _FakeHTTPResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _mock_siteverify(
    monkeypatch: pytest.MonkeyPatch,
    *,
    success: bool = True,
    raise_exc: BaseException | None = None,
    status: int = 200,
) -> list[Any]:
    calls: list[Any] = []

    def fake_urlopen(request: Any, timeout: float = 0):  # noqa: ANN001
        calls.append((request, timeout))
        if raise_exc is not None:
            raise raise_exc
        body = json.dumps({"success": success}).encode()
        return _FakeHTTPResponse(body, status=status)

    monkeypatch.setattr(
        "apps.accounts.services.human_verification.turnstile.urlopen",
        fake_urlopen,
    )
    return calls


@pytest.mark.django_db
@override_settings(TURNSTILE_ENABLED=False)
def test_register_unchanged_when_turnstile_disabled(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "plain@example.com"}),
        content_type="application/json",
    )
    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_REGISTER_RATE="1000/hour",
    AUTH_TURNSTILE_DEGRADED_RATE="1000/hour",
)
def test_register_ok_with_valid_turnstile(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_siteverify(monkeypatch, success=True)
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "ok@example.com", "turnstile_token": "token-ok-1"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert User.objects.filter(email="ok@example.com").exists()


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_REGISTER_RATE="1000/hour",
)
def test_register_fails_without_token(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "missing@example.com"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert "turnstile_token" in response.json()
    assert not User.objects.filter(email="missing@example.com").exists()


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_REGISTER_RATE="1000/hour",
)
def test_register_fails_on_invalid_token(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_siteverify(monkeypatch, success=False)
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "bad@example.com", "turnstile_token": "token-bad"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert not User.objects.filter(email="bad@example.com").exists()


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_REGISTER_RATE="1000/hour",
    TURNSTILE_TOKEN_SEEN_TTL=180,
)
def test_register_rejects_replayed_token(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_siteverify(monkeypatch, success=True)
    payload = json.dumps(
        {"email": "replay@example.com", "turnstile_token": "same-token"}
    )
    first = client.post(
        "/api/v1/auth/register/",
        data=payload,
        content_type="application/json",
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/auth/register/",
        data=json.dumps(
            {"email": "replay2@example.com", "turnstile_token": "same-token"}
        ),
        content_type="application/json",
    )
    assert second.status_code == 400


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_REGISTER_RATE="1000/hour",
    AUTH_TURNSTILE_DEGRADED_RATE="2/hour",
)
def test_unavailable_fail_open_then_degraded_gate(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_siteverify(monkeypatch, raise_exc=URLError("down"))

    for i in range(2):
        response = client.post(
            "/api/v1/auth/register/",
            data=json.dumps(
                {
                    "email": f"degraded{i}@example.com",
                    "turnstile_token": f"tok-deg-{i}",
                }
            ),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content

    blocked = client.post(
        "/api/v1/auth/register/",
        data=json.dumps(
            {"email": "blocked@example.com", "turnstile_token": "tok-deg-block"}
        ),
        content_type="application/json",
    )
    assert blocked.status_code == 429


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    TURNSTILE_BYPASS_SECRET="internal-secret",
    AUTH_REGISTER_RATE="1000/hour",
)
def test_internal_bypass_skips_verify(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "bypass@example.com"}),
        content_type="application/json",
        HTTP_X_ROAMKIT_INTERNAL="internal-secret",
    )
    assert response.status_code == 200
    assert User.objects.filter(email="bypass@example.com").exists()


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    TURNSTILE_BYPASS_SECRET="internal-secret",
    AUTH_REGISTER_RATE="1000/hour",
)
def test_wrong_bypass_still_requires_token(client: Client) -> None:
    response = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "wrongbypass@example.com"}),
        content_type="application/json",
        HTTP_X_ROAMKIT_INTERNAL="wrong",
    )
    assert response.status_code == 400


@pytest.mark.django_db
@override_settings(
    TURNSTILE_ENABLED=True,
    TURNSTILE_SECRET_KEY="test-secret",
    AUTH_TOKEN_RATE="1000/hour",
    AUTH_TURNSTILE_DEGRADED_RATE="1000/hour",
)
def test_login_requires_turnstile_when_enabled(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    User.objects.create_user(email="login@example.com", password=PASSWORD)
    denied = client.post(
        "/api/v1/auth/token/",
        data=json.dumps({"email": "login@example.com", "password": PASSWORD}),
        content_type="application/json",
    )
    assert denied.status_code == 400

    _mock_siteverify(monkeypatch, success=True)
    ok = client.post(
        "/api/v1/auth/token/",
        data=json.dumps(
            {
                "email": "login@example.com",
                "password": PASSWORD,
                "turnstile_token": "login-tok",
            }
        ),
        content_type="application/json",
    )
    assert ok.status_code == 200
    assert "access" in ok.json()


@pytest.mark.django_db
def test_register_throttle_returns_429(client: Client, settings) -> None:
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_THROTTLE_RATES": {
            **settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],
            "auth_register": "2/hour",
        },
    }
    cache.clear()
    for i in range(2):
        response = client.post(
            "/api/v1/auth/register/",
            data=json.dumps({"email": f"throttle{i}@example.com"}),
            content_type="application/json",
        )
        assert response.status_code == 200

    blocked = client.post(
        "/api/v1/auth/register/",
        data=json.dumps({"email": "throttle-block@example.com"}),
        content_type="application/json",
    )
    assert blocked.status_code == 429


def test_health_turnstile_disabled(client: Client) -> None:
    with override_settings(TURNSTILE_ENABLED=False):
        response = client.get("/health/turnstile")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["status"] == "ok"


def test_health_turnstile_enabled_without_secret(client: Client) -> None:
    with override_settings(TURNSTILE_ENABLED=True, TURNSTILE_SECRET_KEY=""):
        response = client.get("/health/turnstile")
    assert response.status_code == 503
    assert response.json()["status"] == "misconfigured"


def test_health_turnstile_enabled_ok(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "core.health.views._hostname_resolvable",
        lambda _hostname: True,
    )
    with override_settings(
        TURNSTILE_ENABLED=True, TURNSTILE_SECRET_KEY="secret-present"
    ):
        response = client.get("/health/turnstile")
    assert response.status_code == 200
    assert response.json()["secret_configured"] is True


def test_client_ip_prefers_cf_connecting_ip() -> None:
    from django.test import RequestFactory

    from core.http.client_ip import get_client_ip

    request = RequestFactory().get(
        "/",
        HTTP_CF_CONNECTING_IP="203.0.113.9",
        HTTP_X_FORWARDED_FOR="198.51.100.1",
        REMOTE_ADDR="10.0.0.1",
    )
    assert get_client_ip(request) == "203.0.113.9"


def test_turnstile_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_siteverify(monkeypatch, raise_exc=TimeoutError())
    with override_settings(TURNSTILE_SECRET_KEY="secret"):
        result = TurnstileVerificationService().verify(
            "tok",
            remoteip="1.2.3.4",
            request_id="rid",
            endpoint="auth_token",
        )
    assert result is HumanVerificationResult.UNAVAILABLE


def test_turnstile_http_5xx_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    err = HTTPError(
        "https://example/siteverify",
        502,
        "Bad Gateway",
        hdrs=None,  # type: ignore[arg-type]
        fp=BytesIO(b""),
    )
    _mock_siteverify(monkeypatch, raise_exc=err)
    with override_settings(TURNSTILE_SECRET_KEY="secret"):
        result = TurnstileVerificationService().verify(
            "tok-5xx",
            remoteip="1.2.3.4",
            request_id="rid",
            endpoint="auth_token",
        )
    assert result is HumanVerificationResult.UNAVAILABLE
