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
