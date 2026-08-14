"""Organization / device-facing DRF throttles."""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework.request import Request
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView


class _DeviceCredentialPostThrottle(SimpleRateThrottle):
    """Shared IP throttle for unauthenticated device credential POSTs."""

    scope: str

    def get_rate(self) -> str | None:
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
        ident = self.get_ident(request)
        if not ident:
            return None
        return self.cache_format % {"scope": self.scope, "ident": ident}


class DeviceStatusRateThrottle(_DeviceCredentialPostThrottle):
    """Limit unauthenticated device status posts per client IP."""

    scope = "device_status"


class DeviceCoverageRateThrottle(_DeviceCredentialPostThrottle):
    """Limit unauthenticated device coverage posts per client IP."""

    scope = "device_coverage"


class DevicePackagesRateThrottle(_DeviceCredentialPostThrottle):
    """Limit unauthenticated device package-history posts per client IP."""

    scope = "device_packages"
