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
