"""Auth-scoped DRF throttles using ``get_client_ip``."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from core import metrics
from core.http.client_ip import get_client_ip


class AuthScopedRateThrottle(SimpleRateThrottle):
    """Scoped throttle keyed by client IP (Cloudflare-aware)."""

    scope: str | None = None

    def get_rate(self) -> str | None:
        if not self.scope:
            return None
        rates = settings.REST_FRAMEWORK.get("DEFAULT_THROTTLE_RATES", {})
        try:
            return rates[self.scope]
        except KeyError as exc:
            msg = (
                f"No default throttle rate set for scope '{self.scope}' "
                "in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']."
            )
            raise ImproperlyConfigured(msg) from exc

    def get_cache_key(self, request: Request, view: APIView) -> str | None:
        if request.method != "POST":
            return None
        ident = get_client_ip(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def throttle_failure(self) -> bool:
        metrics.incr(
            "auth_throttle_block_total",
            scope=self.scope or "unknown",
        )
        return super().throttle_failure()


class AuthTokenRateThrottle(AuthScopedRateThrottle):
    scope = "auth_token"


class AuthRegisterRateThrottle(AuthScopedRateThrottle):
    scope = "auth_register"


class AuthPasswordResetRateThrottle(AuthScopedRateThrottle):
    scope = "auth_password_reset"


class AuthActivateRateThrottle(AuthScopedRateThrottle):
    scope = "auth_activate"


class AuthPasswordResetConfirmRateThrottle(AuthScopedRateThrottle):
    scope = "auth_password_reset_confirm"


class AuthGoogleRateThrottle(AuthScopedRateThrottle):
    scope = "auth_google"
