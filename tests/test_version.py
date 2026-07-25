import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

from config.settings.secrets import require_production_secret


def test_version_returns_release_metadata(client: Client) -> None:
    with override_settings(
        ROAMKIT_GIT_SHA="abc123def",
        ROAMKIT_BUILD_DATE="2026-07-25T00:00:00Z",
        ROAMKIT_IMAGE_TAG="abc123def",
        ROAMKIT_ENVIRONMENT="test",
    ):
        response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {
        "git_sha": "abc123def",
        "build_date": "2026-07-25T00:00:00Z",
        "image_tag": "abc123def",
        "environment": "test",
    }


def test_version_allows_empty_metadata_locally(client: Client) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"git_sha", "build_date", "image_tag", "environment"}


def test_require_production_secret_rejects_placeholder() -> None:
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        require_production_secret("change-me-production")


def test_require_production_secret_rejects_empty() -> None:
    with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
        require_production_secret("")


def test_require_production_secret_accepts_real_value() -> None:
    assert (
        require_production_secret("production-secret-key-with-enough-entropy-32b")
        == "production-secret-key-with-enough-entropy-32b"
    )
