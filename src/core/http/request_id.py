"""Request correlation id helpers."""

from __future__ import annotations

import uuid

from django.http import HttpRequest


def get_or_create_request_id(request: HttpRequest) -> str:
    """Return ``request.request_id``, creating one from header or a new UUID."""
    existing = getattr(request, "request_id", None)
    if existing:
        return str(existing)

    header = (request.META.get("HTTP_X_REQUEST_ID") or "").strip()
    request_id = header or str(uuid.uuid4())
    request.request_id = request_id
    return request_id
