"""Airalo Partner API HTTP client."""

from __future__ import annotations

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


class AiraloClientError(Exception):
    """Raised when the Airalo API returns an error response."""


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


class AiraloClient:
    """Minimal HTTP client for the Airalo Partner API."""

    _token_cache = _TokenCache()

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id or settings.AIRALO_CLIENT_ID
        self.client_secret = client_secret or settings.AIRALO_CLIENT_SECRET
        self.base_url = (base_url or settings.AIRALO_BASE_URL).rstrip("/")
        self.timeout = timeout

    def list_packages(
        self,
        *,
        country_code: str | None = None,
        package_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch the full package catalog from Airalo, following pagination."""
        params: dict[str, str] = {}
        if country_code:
            params["filter[country]"] = country_code
        if package_type:
            params["filter[type]"] = package_type

        items: list[dict[str, Any]] = []
        page = 1
        while True:
            page_params = {**params, "page": str(page)}
            query = f"?{urllib.parse.urlencode(page_params)}"
            payload = self._request("GET", f"/v2/packages{query}")
            data = payload.get("data", [])
            if isinstance(data, list):
                items.extend(data)
            else:
                data = []

            meta = payload.get("meta") or {}
            last_page = int(meta.get("last_page") or meta.get("lastPage") or page)
            links = payload.get("links") or {}
            has_next = bool(links.get("next")) if isinstance(links, dict) else False

            if not data or (page >= last_page and not has_next):
                break
            page += 1

        return items

    def create_order(
        self,
        *,
        package_id: str,
        quantity: int = 1,
        description: str = "",
    ) -> dict[str, Any]:
        """Submit a synchronous eSIM order via POST /v2/orders."""
        data: dict[str, str] = {
            "package_id": package_id,
            "quantity": str(quantity),
            "type": "sim",
        }
        if description:
            data["description"] = description

        payload = self._request("POST", "/v2/orders", data=data)
        return payload.get("data", {})

    def get_usage(self, iccid: str) -> dict[str, Any]:
        """Fetch data/voice/text usage for an eSIM via GET /v2/sims/{iccid}/usage."""
        path = f"/v2/sims/{urllib.parse.quote(iccid, safe='')}/usage"
        payload = self._request("GET", path)
        data = payload.get("data", {})
        return data if isinstance(data, dict) else {}

    def list_topups(self, iccid: str) -> list[dict[str, Any]]:
        """List top-up packages for an eSIM via GET /v2/sims/{iccid}/topups."""
        path = f"/v2/sims/{urllib.parse.quote(iccid, safe='')}/topups"
        payload = self._request("GET", path)
        data = payload.get("data", [])
        return data if isinstance(data, list) else []

    def submit_topup(
        self,
        *,
        iccid: str,
        package_id: str,
        description: str = "",
    ) -> dict[str, Any]:
        """Submit a top-up order via POST /v2/orders/topups."""
        data: dict[str, str] = {
            "iccid": iccid,
            "package_id": package_id,
            "description": description or f"Topup ({iccid})",
        }
        payload = self._request("POST", "/v2/orders/topups", data=data)
        return payload.get("data", {})

    def _get_access_token(self) -> str:
        now = time.time()
        if self._token_cache.access_token and now < self._token_cache.expires_at:
            return self._token_cache.access_token

        response = self._request(
            "POST",
            "/v2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            authenticated=False,
        )
        token = response.get("data", {}).get("access_token") or response.get(
            "access_token", ""
        )
        if not token:
            raise AiraloClientError("Airalo token response missing access_token")

        expires_in = int(response.get("data", {}).get("expires_in") or 86_400)
        self._token_cache.access_token = token
        self._token_cache.expires_at = now + max(expires_in - 60, 60)
        return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        # Cloudflare bans the default Python-urllib User-Agent (error 1010).
        headers = {
            "Accept": "application/json",
            "User-Agent": "roamkit-api/1.0",
        }

        body: bytes | None = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if authenticated:
            headers["Authorization"] = f"Bearer {self._get_access_token()}"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.warning(
                "Airalo API error %s for %s %s: %s",
                exc.code,
                method,
                path,
                detail,
            )
            raise AiraloClientError(
                f"Airalo API returned {exc.code} for {method} {path}"
            ) from exc
        except urllib.error.URLError as exc:
            raise AiraloClientError(f"Airalo API request failed: {exc.reason}") from exc

        if not raw:
            return {}

        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise AiraloClientError("Airalo API returned unexpected JSON payload")
        return parsed
