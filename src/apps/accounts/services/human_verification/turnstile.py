"""Cloudflare Turnstile siteverify implementation."""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

from apps.accounts.services.human_verification.base import (
    HumanVerificationResult,
    HumanVerificationService,
)
from core import metrics

logger = logging.getLogger(__name__)


def hash_ip(ip: str) -> str:
    """Short salted hash for logs — never log raw client IPs."""
    material = f"{settings.SECRET_KEY}:{ip}".encode()
    return hashlib.sha256(material).hexdigest()[:16]


def _seen_cache_key(token: str) -> str:
    digest = hashlib.sha256(token.encode()).hexdigest()
    return f"turnstile:seen:{digest}"


class TurnstileVerificationService(HumanVerificationService):
    """Verify Cloudflare Turnstile tokens with replay guard and fail-open."""

    def verify(
        self,
        token: str | None,
        *,
        remoteip: str,
        request_id: str,
        endpoint: str,
    ) -> HumanVerificationResult:
        if not token or not str(token).strip():
            metrics.incr("turnstile_verify_failed_total", reason="missing_token")
            return HumanVerificationResult.FAIL

        token = str(token).strip()
        ttl = int(getattr(settings, "TURNSTILE_TOKEN_SEEN_TTL", 180))
        if not cache.add(_seen_cache_key(token), 1, timeout=ttl):
            metrics.incr("turnstile_verify_failed_total", reason="replay")
            return HumanVerificationResult.FAIL

        result, reason = self._siteverify(token, remoteip=remoteip)
        if result is HumanVerificationResult.OK:
            metrics.incr("turnstile_verify_success_total")
            return result

        if result is HumanVerificationResult.FAIL:
            metrics.incr("turnstile_verify_failed_total", reason=reason or "invalid")
            return result

        metrics.incr("turnstile_verify_unavailable_total", reason=reason or "unknown")
        logger.warning(
            "turnstile_unavailable request_id=%s ip_hash=%s endpoint=%s reason=%s",
            request_id,
            hash_ip(remoteip),
            endpoint,
            reason,
            extra={
                "request_id": request_id,
                "ip_hash": hash_ip(remoteip),
                "endpoint": endpoint,
                "reason": reason,
            },
        )
        return HumanVerificationResult.UNAVAILABLE

    def _siteverify(
        self, token: str, *, remoteip: str
    ) -> tuple[HumanVerificationResult, str | None]:
        secret = (getattr(settings, "TURNSTILE_SECRET_KEY", "") or "").strip()
        if not secret:
            return HumanVerificationResult.UNAVAILABLE, "network"

        payload: dict[str, str] = {"secret": secret, "response": token}
        if remoteip and remoteip != "unknown":
            payload["remoteip"] = remoteip

        url = getattr(
            settings,
            "TURNSTILE_VERIFY_URL",
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        timeout = float(getattr(settings, "TURNSTILE_VERIFY_TIMEOUT", 2.5))
        data = urlencode(payload).encode()
        request = Request(url, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            # HTTPS siteverify only; URL comes from settings, not user input.
            with urlopen(request, timeout=timeout) as response:  # nosec B310
                body = response.read().decode()
                status = getattr(response, "status", 200)
        except TimeoutError:
            return HumanVerificationResult.UNAVAILABLE, "timeout"
        except HTTPError as exc:
            if 500 <= exc.code <= 599:
                return HumanVerificationResult.UNAVAILABLE, f"http_5xx:{exc.code}"
            return HumanVerificationResult.FAIL, f"http_{exc.code}"
        except URLError:
            return HumanVerificationResult.UNAVAILABLE, "network"
        except OSError:
            return HumanVerificationResult.UNAVAILABLE, "network"

        if status >= 500:
            return HumanVerificationResult.UNAVAILABLE, f"http_5xx:{status}"

        parsed = self._parse_body(body)
        if parsed is None:
            return HumanVerificationResult.UNAVAILABLE, "network"
        if parsed.get("success") is True:
            return HumanVerificationResult.OK, None
        return HumanVerificationResult.FAIL, "invalid"

    @staticmethod
    def _parse_body(body: str) -> dict[str, Any] | None:
        import json

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        return data
