"""Resolve the client IP for throttling and human verification."""

from __future__ import annotations

from django.http import HttpRequest


def get_client_ip(request: HttpRequest) -> str:
    """
    Prefer Cloudflare's ``CF-Connecting-IP``, then the first ``X-Forwarded-For``
    hop, then ``REMOTE_ADDR``.

    Staging/production API traffic is expected behind Cloudflare; a single
    ``CF-Connecting-IP`` value is authoritative when present.
    """
    cf_ip = (request.META.get("HTTP_CF_CONNECTING_IP") or "").strip()
    if cf_ip:
        return cf_ip.split(",")[0].strip()

    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()

    return (request.META.get("REMOTE_ADDR") or "").strip() or "unknown"
