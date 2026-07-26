"""Production settings — strict hosts, cookies, HSTS; fail-fast secrets."""

import os

from config.sentry import init_sentry

from .base import *  # noqa: F403
from .secrets import require_production_secret

DEBUG = False

SECRET_KEY = require_production_secret(os.environ.get("DJANGO_SECRET_KEY"))

# Docker healthchecks and deploy script curl localhost from inside the container.
ALLOWED_HOSTS = list(
    dict.fromkeys([*ALLOWED_HOSTS, "localhost", "127.0.0.1"])  # noqa: F405
)

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = False

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,  # noqa: F405
    }
}

SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

_csrf_origins = os.environ.get(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "https://api.roamkit.net,https://roamkit.net,https://www.roamkit.net",
)
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in _csrf_origins.split(",") if origin.strip()
]

# Prefer explicit production label when unset.
if not ROAMKIT_ENVIRONMENT:  # noqa: F405
    ROAMKIT_ENVIRONMENT = "production"

if TURNSTILE_ENABLED:  # noqa: F405
    _turnstile_secret = (TURNSTILE_SECRET_KEY or "").strip()  # noqa: F405
    if not _turnstile_secret:
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "TURNSTILE_SECRET_KEY must be set when TURNSTILE_ENABLED=true "
            "in production."
        )

init_sentry(environment=ROAMKIT_ENVIRONMENT)
