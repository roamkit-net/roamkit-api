"""OpenAPI schema endpoint smoke tests (C10 infrastructure)."""

from django.test import Client


def test_openapi_schema_endpoint_returns_200(client: Client) -> None:
    response = client.get("/api/schema/")
    assert response.status_code == 200
    # spectacular may return YAML or JSON depending on Accept; body must be non-empty.
    assert len(response.content) > 0


def test_openapi_docs_ui_returns_200(client: Client) -> None:
    response = client.get("/api/docs/")
    assert response.status_code == 200


def test_openapi_redoc_ui_returns_200(client: Client) -> None:
    response = client.get("/api/redoc/")
    assert response.status_code == 200
