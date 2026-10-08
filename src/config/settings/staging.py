"""Staging settings — Traefik proxy headers and hardened defaults."""

import os

from .airalo_guards import parse_blocked_client_ids, validate_staging_airalo
from .base import *  # noqa: F403

DEBUG = False

_base_origins = CORS_ALLOWED_ORIGINS  # noqa: F405
CORS_ALLOWED_ORIGINS = [
    *_base_origins,
    "https://team.staging.roamkit.net",
]
PARTNER_JOIN_BASE_URL = os.environ.get(  # noqa: F405
    "PARTNER_JOIN_BASE_URL", "https://staging.roamkit.net"
).rstrip("/")

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

validate_staging_airalo(
    airalo_sandbox=AIRALO_SANDBOX,  # noqa: F405
    airalo_enabled=AIRALO_ENABLED,  # noqa: F405
    client_id=AIRALO_CLIENT_ID,  # noqa: F405
    client_secret=AIRALO_CLIENT_SECRET,  # noqa: F405
    blocked_client_ids=parse_blocked_client_ids(
        AIRALO_BLOCKED_CLIENT_IDS  # noqa: F405
    ),
)
