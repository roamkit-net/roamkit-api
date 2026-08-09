"""Read-only BlackBerry UEM Cloud REST client (ADR 021 staging proof).

Validated path on tenant S31564560: ``GET /api/v1/devices`` then match
``device.guid``. ``GET /devices/{guid}`` returned 404 on that tenant — do not
assume a detail-by-guid endpoint.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class BlackberryUemClientError(Exception):
    """Raised when UEM REST cannot complete a read-only request."""


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


class BlackberryUemClient:
    """Minimal OAuth client_credentials client for UEM device reads."""

    _token_cache = _TokenCache()

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        host: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str | None = None,
        scope: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.tenant_id = (tenant_id or settings.BLACKBERRY_UEM_TENANT_ID or "").strip()
        self.host = (host or settings.BLACKBERRY_UEM_HOST or "").strip().rstrip("/")
        self.client_id = (client_id or settings.BLACKBERRY_UEM_CLIENT_ID or "").strip()
        self.client_secret = (
            client_secret or settings.BLACKBERRY_UEM_CLIENT_SECRET or ""
        ).strip()
        configured_token = (
            token_url or settings.BLACKBERRY_UEM_TOKEN_URL or ""
        ).strip()
        if configured_token:
            self.token_url = configured_token
        elif self.tenant_id:
            self.token_url = (
                f"https://idp.blackberry.com/op/tenant/{self.tenant_id}/token"
            )
        else:
            self.token_url = ""
        self.scope = (
            scope or settings.BLACKBERRY_UEM_SCOPE or "openid MDMBWS.All"
        ).strip()
        self.timeout = (
            float(timeout)
            if timeout is not None
            else float(getattr(settings, "BLACKBERRY_UEM_TIMEOUT", 30))
        )

    def _ensure_ready(self) -> None:
        if not getattr(settings, "BLACKBERRY_UEM_ENABLED", False):
            raise BlackberryUemClientError(
                "BlackBerry UEM integration is disabled (BLACKBERRY_UEM_ENABLED=false)"
            )
        missing = [
            name
            for name, value in (
                ("BLACKBERRY_UEM_TENANT_ID", self.tenant_id),
                ("BLACKBERRY_UEM_HOST", self.host),
                ("BLACKBERRY_UEM_CLIENT_ID", self.client_id),
                ("BLACKBERRY_UEM_CLIENT_SECRET", self.client_secret),
                ("token_url", self.token_url),
            )
            if not value
        ]
        if missing:
            raise BlackberryUemClientError(
                "BlackBerry UEM is not configured: " + ", ".join(missing)
            )

    @property
    def api_base(self) -> str:
        host = self.host
        if host.startswith("https://") or host.startswith("http://"):
            return f"{host}/{self.tenant_id}/api/v1"
        return f"https://{host}/{self.tenant_id}/api/v1"

    def get_device_by_guid(self, device_guid: str) -> dict[str, Any] | None:
        """Return one device dict matching ``guid``, or None if absent."""
        guid = (device_guid or "").strip().lower()
        if not guid:
            return None
        for device in self.list_devices():
            if str(device.get("guid") or "").strip().lower() == guid:
                return device
        return None

    def list_devices(self) -> list[dict[str, Any]]:
        """GET /devices (read-only)."""
        payload = self._request(
            "GET",
            "/devices",
            accept="application/vnd.blackberry.devices-v1+json",
        )
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise BlackberryUemClientError("UEM /devices response missing devices list")
        return [d for d in devices if isinstance(d, dict)]

    def _get_access_token(self) -> str:
        now = time.time()
        if self._token_cache.access_token and now < self._token_cache.expires_at:
            return self._token_cache.access_token

        # BlackBerry Postman samples: URL-encode secret before Basic auth.
        enc_secret = urllib.parse.quote(self.client_secret, safe="")
        basic = base64.b64encode(f"{self.client_id}:{enc_secret}".encode()).decode()
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": self.scope}
        ).encode()
        request = urllib.request.Request(
            self.token_url,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "roamkit-api/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:  # nosec B310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.warning("UEM token error %s: %s", exc.code, detail)
            raise BlackberryUemClientError(
                f"UEM token endpoint returned {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BlackberryUemClientError(
                f"UEM token request failed: {exc.reason}"
            ) from exc

        parsed = json.loads(raw) if raw else {}
        token = parsed.get("access_token") if isinstance(parsed, dict) else None
        if not token:
            raise BlackberryUemClientError("UEM token response missing access_token")
        expires_in = int(parsed.get("expires_in") or 3600)
        self._token_cache.access_token = token
        self._token_cache.expires_at = now + max(expires_in - 60, 60)
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/json",
    ) -> dict[str, Any]:
        self._ensure_ready()
        url = f"{self.api_base}{path}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._get_access_token()}",
                "Accept": accept,
                "User-Agent": "roamkit-api/1.0",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout
            ) as response:  # nosec B310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "UEM API error %s for %s %s: %s",
                exc.code,
                method,
                path,
                detail,
            )
            raise BlackberryUemClientError(
                f"UEM API returned {exc.code} for {method} {path}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BlackberryUemClientError(
                f"UEM API request failed: {exc.reason}"
            ) from exc

        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise BlackberryUemClientError("UEM API returned unexpected JSON payload")
        return parsed
