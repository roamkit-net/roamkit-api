"""Polygon JSON-RPC HTTP client with timeout, retries, and backoff."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from django.conf import settings

from shared.providers.blockchain import BlockchainRPCError

logger = logging.getLogger(__name__)


class PolygonRpcClient:
    """Minimal JSON-RPC client for Polygon (or compatible) HTTP endpoints."""

    def __init__(
        self,
        *,
        rpc_url: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        backoff_base: float | None = None,
    ) -> None:
        self.rpc_url = (rpc_url or settings.POLYGON_RPC_URL).rstrip("/")
        self.timeout = (
            float(settings.POLYGON_RPC_TIMEOUT) if timeout is None else timeout
        )
        self.retries = int(settings.POLYGON_RPC_RETRIES) if retries is None else retries
        self.backoff_base = (
            float(settings.POLYGON_RPC_BACKOFF_BASE)
            if backoff_base is None
            else backoff_base
        )
        self._next_id = 1

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Invoke a JSON-RPC method with retries and exponential backoff.

        ``retries`` is the number of re-attempts after the first failure
        (default 3 → up to 4 total attempts). Non-retryable JSON-RPC
        application errors raise immediately.
        """
        if not self.rpc_url:
            raise BlockchainRPCError("POLYGON_RPC_URL is not configured")

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params or [],
        }
        self._next_id += 1

        last_error: Exception | None = None
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                return self._request_once(payload)
            except _RetryableRPCError as exc:
                last_error = exc
                logger.warning(
                    "Polygon RPC %s attempt %s/%s failed: %s",
                    method,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt >= attempts:
                    break
                delay = self.backoff_base * (2 ** (attempt - 1))
                time.sleep(delay)

        raise BlockchainRPCError(
            f"Polygon RPC {method} failed after {attempts} attempts"
        ) from last_error

    def _request_once(self, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.rpc_url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "roamkit-api/1.0",
            },
            method="POST",
        )

        try:
            # rpc_url comes from trusted settings/env, not user input.
            with urllib.request.urlopen(  # nosec B310
                request, timeout=self.timeout
            ) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise _RetryableRPCError(f"timeout after {self.timeout}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code >= 500 or exc.code == 429:
                raise _RetryableRPCError(f"HTTP {exc.code}: {detail[:200]}") from exc
            raise BlockchainRPCError(
                f"Polygon RPC HTTP {exc.code}: {detail[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise _RetryableRPCError(f"connection error: {exc.reason}") from exc

        if not raw:
            raise _RetryableRPCError("empty RPC response")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _RetryableRPCError("invalid JSON in RPC response") from exc

        if not isinstance(parsed, dict):
            raise BlockchainRPCError("unexpected JSON-RPC payload type")

        if "error" in parsed and parsed["error"] is not None:
            error = parsed["error"]
            message = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            code = error.get("code") if isinstance(error, dict) else None
            # Retry transient node / rate-limit style errors.
            if code in (-32000, -32005, -32016) or "rate limit" in message.lower():
                raise _RetryableRPCError(message)
            raise BlockchainRPCError(f"JSON-RPC error: {message}")

        return parsed.get("result")


class _RetryableRPCError(Exception):
    """Internal marker for errors that should be retried."""
