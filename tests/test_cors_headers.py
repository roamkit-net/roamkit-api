"""CORS preflight must allow headers used by roamkit-web clients."""

from django.test import Client


def test_cors_preflight_allows_if_match_for_auto_topup(client: Client) -> None:
    """Browser PUT/DELETE send If-Match; omitting it from allow-list breaks saves."""
    response = client.options(
        "/api/v1/me/esims/1/auto-topup/",
        HTTP_ORIGIN="https://roamkit.net",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="PUT",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization,content-type,if-match",
    )

    assert response.status_code == 200
    allowed = {
        h.strip().lower()
        for h in response.headers.get("Access-Control-Allow-Headers", "").split(",")
        if h.strip()
    }
    assert "if-match" in allowed
    assert "authorization" in allowed
    assert "content-type" in allowed


def test_cors_allow_headers_setting_includes_if_match() -> None:
    from django.conf import settings

    allowed = {h.lower() for h in settings.CORS_ALLOW_HEADERS}
    assert "if-match" in allowed
    assert "x-request-id" in allowed


WWW_ORIGIN = "https://www.roamkit.net"


def _allow_list(header_value: str) -> set[str]:
    return {part.strip().lower() for part in header_value.split(",") if part.strip()}


def test_cors_allowed_origins_includes_www() -> None:
    from django.conf import settings

    assert WWW_ORIGIN in settings.CORS_ALLOWED_ORIGINS


def test_team_origins_are_split_by_environment() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "config" / "settings"
    staging = (root / "staging.py").read_text()
    production = (root / "production.py").read_text()
    base = (root / "base.py").read_text()
    assert "https://team.staging.roamkit.net" in staging
    assert "https://team.roamkit.net" not in staging
    assert "https://team.roamkit.net" in production
    assert "https://team.staging.roamkit.net" not in production
    assert "https://team.roamkit.net" not in base
    assert "https://team.staging.roamkit.net" not in base


def test_cors_expose_headers_names_partner_role() -> None:
    from django.conf import settings

    assert "X-Partner-Role" in settings.CORS_EXPOSE_HEADERS


def test_team_origin_receives_expose_headers_without_the_role(client: Client) -> None:
    """The browser may read X-Partner-Role only when a partner read sets it."""
    from django.test import override_settings

    origin = "https://team.roamkit.net"
    with override_settings(
        CORS_ALLOWED_ORIGINS=[
            "http://localhost:3000",
            "https://staging.roamkit.net",
            "https://roamkit.net",
            "https://www.roamkit.net",
            origin,
        ]
    ):
        response = client.get("/api/v1/billing/config/", HTTP_ORIGIN=origin)

    assert response.headers.get("Access-Control-Allow-Origin") == origin
    assert "x-partner-role" in _allow_list(
        response.headers.get("Access-Control-Expose-Headers", "")
    )
    assert response.headers.get("X-Partner-Role") is None


def test_cors_preflight_allows_www_origin_for_billing_config(client: Client) -> None:
    """www GET /billing/config/ must receive ACAO or catalog prices stay skeleton."""
    response = client.options(
        "/api/v1/billing/config/",
        HTTP_ORIGIN=WWW_ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
    )

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == WWW_ORIGIN


def test_cors_preflight_allows_www_origin_for_google_auth(client: Client) -> None:
    """Browser POST /auth/google/ from www needs full preflight, not only ACAO."""
    response = client.options(
        "/api/v1/auth/google/",
        HTTP_ORIGIN=WWW_ORIGIN,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,accept",
    )

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == WWW_ORIGIN
    assert "post" in _allow_list(
        response.headers.get("Access-Control-Allow-Methods", "")
    )
    assert "content-type" in _allow_list(
        response.headers.get("Access-Control-Allow-Headers", "")
    )
