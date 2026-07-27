"""Static file serving contract for staging/production admin UI."""

from django.conf import settings


def test_whitenoise_is_enabled_for_static_serving() -> None:
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in settings.MIDDLEWARE
    assert settings.STATIC_ROOT
    backend = settings.STORAGES["staticfiles"]["BACKEND"]
    assert "whitenoise" in backend
